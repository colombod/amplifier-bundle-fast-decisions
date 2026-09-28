"""Tests for the fresh `holdout3` S1 split added to scripts/battery_tasks.py.

Reference solutions and plausible-wrong solutions for the 9 new `holdout3`
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

It also proves the 3 new `m-holdout3` multi-turn scenarios (built only
from these 12 holdout3 tasks) pass end-to-end when every subtask's
reference solution is placed in its t{i}-{name}/ subdirectory, and fail
when one subtask is left wrong.
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
# holdout3 reference solutions -- TEST-ONLY, never shipped in Task.files.
# ---------------------------------------------------------------------------

_REFERENCE_SOLUTIONS_H3: dict[str, dict[str, str]] = {
    "repair_base36_encode": {
        "solution.py": '''def base36_encode(n):
    if isinstance(n, bool) or not isinstance(n, int):
        raise TypeError("n must be an int")
    if n < 0:
        raise ValueError("n must be nonnegative")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = []
    x = n
    while x > 0:
        x, r = divmod(x, 36)
        out.append(digits[r])
    return "".join(reversed(out))
''',
    },
    "repair_ipv4_cidr_contains": {
        "solution.py": '''def _parse_ipv4(s):
    if not isinstance(s, str):
        raise TypeError("ip must be a string")
    parts = s.split(".")
    if len(parts) != 4:
        raise ValueError("invalid ipv4 address")
    octets = []
    for p in parts:
        if not p or not p.isdigit():
            raise ValueError("invalid ipv4 address")
        if len(p) > 1 and p[0] == "0":
            raise ValueError("invalid ipv4 address")
        v = int(p)
        if v > 255:
            raise ValueError("invalid ipv4 address")
        octets.append(v)
    return (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]


def cidr_contains(ip, cidr):
    if not isinstance(ip, str) or not isinstance(cidr, str):
        raise TypeError("ip and cidr must be strings")
    if "/" not in cidr:
        raise ValueError("cidr must contain a prefix length")
    net_part, _, prefix_part = cidr.partition("/")
    if not prefix_part or not prefix_part.isdigit():
        raise ValueError("invalid prefix length")
    if len(prefix_part) > 1 and prefix_part[0] == "0":
        raise ValueError("invalid prefix length")
    prefix = int(prefix_part)
    if prefix > 32:
        raise ValueError("invalid prefix length")
    ip_int = _parse_ipv4(ip)
    net_int = _parse_ipv4(net_part)
    mask = 0 if prefix == 0 else (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (ip_int & mask) == (net_int & mask)
''',
    },
    "repair_grid_path_count": {
        "solution.py": '''from grid_util import is_rectangular_grid
from grid_config import MAX_ROWS, MAX_COLS


def count_paths(grid):
    if not isinstance(grid, list):
        raise TypeError("grid must be a list of lists")
    if not grid:
        raise ValueError("grid must have at least one row")
    if not all(isinstance(row, list) for row in grid):
        raise TypeError("each row must be a list")
    if not is_rectangular_grid(grid):
        raise ValueError("grid must be rectangular")
    rows = len(grid)
    cols = len(grid[0])
    if cols == 0:
        raise ValueError("grid must have at least one column")
    if rows > MAX_ROWS or cols > MAX_COLS:
        raise ValueError("grid exceeds maximum dimensions")
    for row in grid:
        for cell in row:
            if isinstance(cell, bool) or not isinstance(cell, int) or cell not in (0, 1):
                raise TypeError("cells must be plain int 0 or 1")
    if grid[0][0] == 1 or grid[rows - 1][cols - 1] == 1:
        return 0
    dp = [[0] * cols for _ in range(rows)]
    dp[0][0] = 1
    for i in range(rows):
        for j in range(cols):
            if i == 0 and j == 0:
                continue
            if grid[i][j] == 1:
                continue
            total = 0
            if i > 0:
                total += dp[i - 1][j]
            if j > 0:
                total += dp[i][j - 1]
            dp[i][j] = total
    return dp[rows - 1][cols - 1]
''',
    },
    "edit_stack_peek_default": {
        "stack_util.py": '''class Stack:
    def __init__(self):
        self._items = []

    def push(self, item):
        self._items.append(item)

    def pop(self):
        if not self._items:
            raise IndexError("pop from empty stack")
        return self._items.pop()

    def is_empty(self):
        return len(self._items) == 0

    def size(self):
        return len(self._items)

    def peek(self, default=None):
        if not self._items:
            return default
        return self._items[-1]
''',
    },
    "edit_email_mask_domain": {
        "mask_util.py": '''def mask_email(email):
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = "*" * len(local)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]
    return masked + "@" + domain
''',
    },
    "edit_inventory_apply_discount_tier": {
        "inventory.py": '''from pricing_tiers import discount_rate_for_quantity


class Inventory:
    def __init__(self):
        self._stock = {}

    def add_stock(self, sku, unit_price, quantity):
        if unit_price < 0 or quantity < 0:
            raise ValueError("invalid stock")
        self._stock[sku] = {"unit_price": unit_price, "quantity": quantity}

    def unit_price(self, sku):
        if sku not in self._stock:
            raise KeyError(sku)
        return self._stock[sku]["unit_price"]

    def order_cost(self, sku, quantity):
        if sku not in self._stock:
            raise KeyError(sku)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return round(self.unit_price(sku) * quantity, 2)

    def order_cost_with_discount(self, sku, quantity):
        base = self.order_cost(sku, quantity)
        rate = discount_rate_for_quantity(quantity)
        return round(base * (1 - rate), 2)
''',
    },
    "bugfix_binary_search_bug": {
        "search_util.py": '''def binary_search(items, target):
    if not isinstance(items, list):
        raise TypeError("items must be a list")
    if isinstance(target, bool) or not isinstance(target, int):
        raise TypeError("target must be an int")
    for i in range(1, len(items)):
        if items[i] <= items[i - 1]:
            raise ValueError("items must be strictly increasing")
    lo, hi = 0, len(items) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if items[mid] == target:
            return mid
        elif items[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
''',
    },
    "bugfix_palindrome_ignore_spaces_bug": {
        "text_check.py": '''def is_palindrome(s):
    if not isinstance(s, str):
        raise TypeError("s must be a string")
    normalized = s.lower().replace(" ", "")
    return normalized == normalized[::-1]
''',
    },
    "bugfix_currency_convert_rounding": {
        "currency.py": '''from rate_table import rate_for


def convert_from_usd(amount_usd, currency):
    if amount_usd < 0:
        raise ValueError("amount_usd must be nonnegative")
    rate = rate_for(currency)
    return round(amount_usd * rate, 2)
''',
    },
}

_WRONG_SOLUTIONS_H3: dict[str, dict[str, str]] = {
    "repair_base36_encode": {
        # Correctly reverses the digits but forgets to reject bool -- accepts
        # True as if it were 1, so "bool-true" boundary passes but the
        # boundary itself is invalid input that must raise TypeError.
        "solution.py": '''def base36_encode(n):
    if not isinstance(n, int):
        raise TypeError("n must be an int")
    if n < 0:
        raise ValueError("n must be nonnegative")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = []
    x = n
    while x > 0:
        x, r = divmod(x, 36)
        out.append(digits[r])
    return "".join(reversed(out))
''',
    },
    "repair_ipv4_cidr_contains": {
        # Validates octet ranges but forgets to reject a leading-zero octet
        # (e.g. "010"), silently parsing it as decimal 10 instead.
        "solution.py": '''def _parse_ipv4(s):
    if not isinstance(s, str):
        raise TypeError("ip must be a string")
    parts = s.split(".")
    if len(parts) != 4:
        raise ValueError("invalid ipv4 address")
    octets = []
    for p in parts:
        if not p or not p.isdigit():
            raise ValueError("invalid ipv4 address")
        v = int(p)
        if v > 255:
            raise ValueError("invalid ipv4 address")
        octets.append(v)
    return (octets[0] << 24) | (octets[1] << 16) | (octets[2] << 8) | octets[3]


def cidr_contains(ip, cidr):
    if not isinstance(ip, str) or not isinstance(cidr, str):
        raise TypeError("ip and cidr must be strings")
    if "/" not in cidr:
        raise ValueError("cidr must contain a prefix length")
    net_part, _, prefix_part = cidr.partition("/")
    if not prefix_part or not prefix_part.isdigit():
        raise ValueError("invalid prefix length")
    prefix = int(prefix_part)
    if prefix > 32:
        raise ValueError("invalid prefix length")
    ip_int = _parse_ipv4(ip)
    net_int = _parse_ipv4(net_part)
    mask = 0 if prefix == 0 else (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
    return (ip_int & mask) == (net_int & mask)
''',
    },
    "repair_grid_path_count": {
        # Handles obstacles and validation but forgets to check whether the
        # start cell itself is blocked, so it overcounts on that boundary.
        "solution.py": '''from grid_util import is_rectangular_grid
from grid_config import MAX_ROWS, MAX_COLS


def count_paths(grid):
    if not isinstance(grid, list):
        raise TypeError("grid must be a list of lists")
    if not grid:
        raise ValueError("grid must have at least one row")
    if not all(isinstance(row, list) for row in grid):
        raise TypeError("each row must be a list")
    if not is_rectangular_grid(grid):
        raise ValueError("grid must be rectangular")
    rows = len(grid)
    cols = len(grid[0])
    if cols == 0:
        raise ValueError("grid must have at least one column")
    if rows > MAX_ROWS or cols > MAX_COLS:
        raise ValueError("grid exceeds maximum dimensions")
    for row in grid:
        for cell in row:
            if isinstance(cell, bool) or not isinstance(cell, int) or cell not in (0, 1):
                raise TypeError("cells must be plain int 0 or 1")
    dp = [[0] * cols for _ in range(rows)]
    dp[0][0] = 1  # BUG (wrong-solution): doesn't check if start cell is blocked
    for i in range(rows):
        for j in range(cols):
            if i == 0 and j == 0:
                continue
            if grid[i][j] == 1:
                continue
            total = 0
            if i > 0:
                total += dp[i - 1][j]
            if j > 0:
                total += dp[i][j - 1]
            dp[i][j] = total
    return dp[rows - 1][cols - 1]
''',
    },
    "edit_stack_peek_default": {
        # Implements peek(default=None) but accidentally removes the item
        # (uses pop() semantics instead of a nondestructive read).
        "stack_util.py": '''class Stack:
    def __init__(self):
        self._items = []

    def push(self, item):
        self._items.append(item)

    def pop(self):
        if not self._items:
            raise IndexError("pop from empty stack")
        return self._items.pop()

    def is_empty(self):
        return len(self._items) == 0

    def size(self):
        return len(self._items)

    def peek(self, default=None):
        if not self._items:
            return default
        return self._items.pop()  # BUG (wrong-solution): removes the item instead of just reading it
''',
    },
    "edit_email_mask_domain": {
        # Masks the middle but also masks the last character for the "== 2"
        # boundary in a different, incorrect way (masks 3+ length local parts
        # correctly but treats length-2 locals as if they were length>=3,
        # revealing a fabricated middle asterisk count of 0 -- i.e. leaves a
        # 2-char local fully unmasked).
        "mask_util.py": '''def mask_email(email):
    local, _, domain = email.partition("@")
    if len(local) <= 1:
        masked = "*" * len(local)
    else:
        masked = local[0] + "*" * (len(local) - 2) + local[-1]  # BUG (wrong-solution): 2-char locals leak both chars
    return masked + "@" + domain
''',
    },
    "edit_inventory_apply_discount_tier": {
        # Applies the discount but rounds before subtracting from 1.0 instead
        # of rounding the final result, drifting from the spec on some tiers
        # (still passes the exact-tier-boundary examples used in the public
        # check, but fails validation ordering: raises the KeyError check
        # AFTER computing the discount rate instead of before order_cost).
        "inventory.py": '''from pricing_tiers import discount_rate_for_quantity


class Inventory:
    def __init__(self):
        self._stock = {}

    def add_stock(self, sku, unit_price, quantity):
        if unit_price < 0 or quantity < 0:
            raise ValueError("invalid stock")
        self._stock[sku] = {"unit_price": unit_price, "quantity": quantity}

    def unit_price(self, sku):
        if sku not in self._stock:
            raise KeyError(sku)
        return self._stock[sku]["unit_price"]

    def order_cost(self, sku, quantity):
        if sku not in self._stock:
            raise KeyError(sku)
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return round(self.unit_price(sku) * quantity, 2)

    def order_cost_with_discount(self, sku, quantity):
        # BUG (wrong-solution): computes the discount rate before validating
        # quantity, so a nonpositive quantity raises KeyError-vs-ValueError
        # in the wrong order when sku is also unknown -- but concretely, it
        # skips order_cost's validation path entirely by inlining the math.
        rate = discount_rate_for_quantity(quantity)
        base = round(self.unit_price(sku) * quantity, 2)
        return round(base * (1 - rate), 2)
''',
    },
    "bugfix_binary_search_bug": {
        # Fixes the off-by-one but drops the strictly-increasing validation
        # while rewriting the function -- a plausible slip.
        "search_util.py": '''def binary_search(items, target):
    if not isinstance(items, list):
        raise TypeError("items must be a list")
    if isinstance(target, bool) or not isinstance(target, int):
        raise TypeError("target must be an int")
    lo, hi = 0, len(items) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if items[mid] == target:
            return mid
        elif items[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
''',
    },
    "bugfix_palindrome_ignore_spaces_bug": {
        # Over-fixes: strips all punctuation in addition to spaces, which
        # the spec explicitly says must be treated literally.
        "text_check.py": '''import re


def is_palindrome(s):
    if not isinstance(s, str):
        raise TypeError("s must be a string")
    normalized = re.sub(r"[^a-z0-9]", "", s.lower())  # BUG (wrong-solution): also strips punctuation
    return normalized == normalized[::-1]
''',
    },
    "bugfix_currency_convert_rounding": {
        # Fixes the rounding but, while rewriting the function, drops the
        # nonnegative-amount validation -- a plausible slip that passes every
        # happy-path case but fails the negative-amount boundary.
        "currency.py": '''from rate_table import rate_for


def convert_from_usd(amount_usd, currency):
    rate = rate_for(currency)
    return round(amount_usd * rate, 2)  # BUG (wrong-solution): lost the nonnegative validation
''',
    },
}

_HOLDOUT3_CODE_TASK_NAMES = [
    "repair_base36_encode",
    "repair_ipv4_cidr_contains",
    "repair_grid_path_count",
    "edit_stack_peek_default",
    "edit_email_mask_domain",
    "edit_inventory_apply_discount_tier",
    "bugfix_binary_search_bug",
    "bugfix_palindrome_ignore_spaces_bug",
    "bugfix_currency_convert_rounding",
]

_HOLDOUT3_ANSWER_TASK_NAMES = [
    "answer_schema_version",
    "answer_max_connections",
    "answer_primary_backup_region",
]

_HOLDOUT3_LONG_TASKS = {
    "repair_grid_path_count",
    "edit_inventory_apply_discount_tier",
    "bugfix_currency_convert_rounding",
    "answer_primary_backup_region",
}

_HOLDOUT3_SCENARIO_NAMES = ("scn_holdout3_1", "scn_holdout3_2", "scn_holdout3_3")


class TestHoldout3RegistryShape(unittest.TestCase):
    def test_twelve_holdout3_tasks_three_per_family(self):
        holdout3 = bt.split("holdout3")
        self.assertEqual(len(holdout3), 12)
        by_family = {}
        for name in holdout3:
            t = bt.TASKS[name]
            self.assertEqual(t.split, "holdout3", name)
            by_family.setdefault(t.family, 0)
            by_family[t.family] += 1
        self.assertEqual(by_family, {"repair": 3, "edit": 3, "bugfix": 3, "answer": 3})

    def test_holdout3_names_distinct_from_all_other_tasks(self):
        holdout3 = set(bt.split("holdout3"))
        others = set(bt.TASKS.keys()) - holdout3
        self.assertEqual(holdout3 & others, set())
        self.assertEqual(len(holdout3), len(_HOLDOUT3_CODE_TASK_NAMES) + len(_HOLDOUT3_ANSWER_TASK_NAMES))

    def test_all_expected_names_present(self):
        holdout3 = set(bt.split("holdout3"))
        expected = set(_HOLDOUT3_CODE_TASK_NAMES) | set(_HOLDOUT3_ANSWER_TASK_NAMES)
        self.assertEqual(holdout3, expected)

    def test_one_long_task_per_family(self):
        by_family = {}
        for name in _HOLDOUT3_LONG_TASKS:
            t = bt.TASKS[name]
            by_family.setdefault(t.family, 0)
            by_family[t.family] += 1
        self.assertEqual(by_family, {"repair": 1, "edit": 1, "bugfix": 1, "answer": 1})
        # The long tasks read/edit noticeably more: more files than their
        # family's other holdout3 tasks.
        for name in _HOLDOUT3_LONG_TASKS:
            long_file_count = len(bt.TASKS[name].files)
            family = bt.TASKS[name].family
            siblings = [
                n for n in bt.split("holdout3")
                if bt.TASKS[n].family == family and n != name
            ]
            for sib in siblings:
                self.assertGreater(
                    long_file_count, len(bt.TASKS[sib].files),
                    f"{name} should read strictly more files than {sib}",
                )

    def test_protected_files_match_existing_convention(self):
        for name in _HOLDOUT3_CODE_TASK_NAMES:
            t = bt.TASKS[name]
            self.assertIn("README.md", t.protected, name)
            if "test_public.py" in t.files:
                self.assertIn("test_public.py", t.protected, name)

    def test_reference_solutions_not_in_task_files_or_module_dict(self):
        # Requirement: reference solutions must never be readable from an
        # agent's workspace. Task.files is exactly what gets copied there.
        for name, overrides in _REFERENCE_SOLUTIONS_H3.items():
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


class TestHoldout3CodeTasks(unittest.TestCase):
    def test_starter_fails_reference_passes_wrong_fails(self):
        for name in _HOLDOUT3_CODE_TASK_NAMES:
            task = bt.TASKS[name]
            with self.subTest(task=name):
                ws = _materialize(task.files)
                try:
                    result = task.evaluate(ws)
                    self.assertGreater(result["failed"], 0, f"{name}: starter should fail evaluate, got {result}")
                finally:
                    shutil.rmtree(ws, ignore_errors=True)

                overrides = _REFERENCE_SOLUTIONS_H3[name]
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

                wrong_overrides = _WRONG_SOLUTIONS_H3[name]
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
        for name in _HOLDOUT3_CODE_TASK_NAMES:
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


class TestHoldout3AnswerTasks(unittest.TestCase):
    _CORRECT = {
        "answer_schema_version": ["3", "`3`"],
        "answer_max_connections": ["6", "six", "The limit is 6."],
        "answer_primary_backup_region": ["eu-west-3", "EU-WEST-3", "`eu-west-3`"],
    }
    _DECOYS = {
        "answer_schema_version": ["2", "1", "30"],
        "answer_max_connections": ["10", "3", "4"],
        "answer_primary_backup_region": ["eu-west-1", "eu-west-2", "us-east-1", "us-east-2", "eu-north-1"],
    }

    def test_answer_tasks_are_answer_kind_with_zero_check_evaluate(self):
        for name in _HOLDOUT3_ANSWER_TASK_NAMES:
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


# Literal correct answer tokens for the 3 holdout3 answer tasks -- used (not
# the raw expected_answer regex, which is not itself a literal answer string)
# when building a scenario turn's final ANSWER: message.
_HOLDOUT3_ANSWER_LITERAL = {
    "answer_schema_version": "3",
    "answer_max_connections": "6",
    "answer_primary_backup_region": "eu-west-3",
}


class TestMHoldout3Scenarios(unittest.TestCase):
    """m-holdout3 multi-turn scenarios (scn_holdout3_1..3): registered
    correctly, and pass/fail end-to-end exactly like the m-dev scenarios in
    tests/test_battery_tasks.py::TestScenarios, but built from holdout3
    tasks whose reference/wrong solutions live only in this file.
    """

    def _materialize_scenario(self, task) -> Path:
        d = Path(tempfile.mkdtemp())
        for relpath, content in task.files.items():
            path = d / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return d

    def test_scenarios_registered_under_m_holdout3_with_four_subtasks(self):
        self.assertEqual(set(bt.split("m-holdout3")), set(_HOLDOUT3_SCENARIO_NAMES))
        for name in _HOLDOUT3_SCENARIO_NAMES:
            task = bt.TASKS[name]
            self.assertEqual(task.kind, "scenario")
            self.assertEqual(task.family, "scenario")
            self.assertEqual(task.split, "m-holdout3")
            self.assertEqual(len(task.subtasks), 4)
            families_in_order = [bt.TASKS[s].family for s in task.subtasks]
            self.assertEqual(families_in_order, ["repair", "answer", "edit", "bugfix"])

    def test_fully_correct_scenario_passes(self):
        for name in _HOLDOUT3_SCENARIO_NAMES:
            task = bt.TASKS[name]
            with self.subTest(scenario=name):
                ws = self._materialize_scenario(task)
                try:
                    final_messages = {}
                    for i, subtask_name in enumerate(task.subtasks, start=1):
                        directory = bt.scenario_turn_dir(i, subtask_name)
                        if subtask_name in _REFERENCE_SOLUTIONS_H3:
                            for relpath, content in _REFERENCE_SOLUTIONS_H3[subtask_name].items():
                                (ws / directory / relpath).write_text(content, encoding="utf-8")
                        subtask = bt.TASKS[subtask_name]
                        if subtask.kind == "answer":
                            final_messages[i] = f"ANSWER: {_HOLDOUT3_ANSWER_LITERAL[subtask_name]}"
                    parts = []
                    for i, subtask_name in enumerate(task.subtasks, start=1):
                        directory = bt.scenario_turn_dir(i, subtask_name)
                        q = bt.evaluate_scenario_turn(subtask_name, ws / directory, final_messages.get(i))
                        parts.append((directory, q))
                    folded = bt.fold_scenario_qualities(parts)
                    self.assertEqual(folded["failed"], 0, folded)
                    self.assertGreater(folded["checks"], 0)
                finally:
                    shutil.rmtree(ws, ignore_errors=True)

    def test_partially_wrong_scenario_fails_with_turn_prefixed_label(self):
        """Leave turn 1 (the repair subtask) unfixed; everything else
        correct. The fold must fail, and the failing label must be
        traceable to turn 1's own directory."""
        for name in _HOLDOUT3_SCENARIO_NAMES:
            task = bt.TASKS[name]
            with self.subTest(scenario=name):
                ws = self._materialize_scenario(task)
                try:
                    final_messages = {}
                    for i, subtask_name in enumerate(task.subtasks, start=1):
                        if i == 1:
                            continue  # leave the repair starter broken
                        directory = bt.scenario_turn_dir(i, subtask_name)
                        if subtask_name in _REFERENCE_SOLUTIONS_H3:
                            for relpath, content in _REFERENCE_SOLUTIONS_H3[subtask_name].items():
                                (ws / directory / relpath).write_text(content, encoding="utf-8")
                        subtask = bt.TASKS[subtask_name]
                        if subtask.kind == "answer":
                            final_messages[i] = f"ANSWER: {_HOLDOUT3_ANSWER_LITERAL[subtask_name]}"
                    parts = []
                    for i, subtask_name in enumerate(task.subtasks, start=1):
                        directory = bt.scenario_turn_dir(i, subtask_name)
                        q = bt.evaluate_scenario_turn(subtask_name, ws / directory, final_messages.get(i))
                        parts.append((directory, q))
                    folded = bt.fold_scenario_qualities(parts)
                    self.assertGreater(folded["failed"], 0, folded)
                    turn1_dir = bt.scenario_turn_dir(1, task.subtasks[0])
                    self.assertTrue(
                        any(label.startswith(f"{turn1_dir}:") for label in folded["failure_labels"]), folded
                    )
                finally:
                    shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
