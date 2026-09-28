"""Tests for scripts/battery_tasks.py -- the 20-task coding-agent battery."""
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


class TestRegistryShape(unittest.TestCase):
    def test_exactly_fortyfour_tasks_eleven_per_family(self):
        # 20 original (dev + holdout) + 12 fresh holdout2 tasks (3/family) +
        # 12 fresh holdout3 tasks (3/family), plus 3 multi-turn m-dev
        # scenarios + 3 multi-turn m-holdout3 scenarios (family="scenario",
        # built from existing dev/holdout3 tasks -- see TestScenarios below).
        self.assertEqual(len(bt.TASKS), 50)
        counts = {}
        for t in bt.TASKS.values():
            counts[t.family] = counts.get(t.family, 0) + 1
        self.assertEqual(counts, {"repair": 11, "edit": 11, "bugfix": 11, "answer": 11, "scenario": 6})

    def test_three_dev_two_holdout_three_holdout2_three_holdout3_per_family(self):
        # scenario tasks (family="scenario") live in the m-dev/m-holdout3
        # splits, which have their own 3-scenario shapes (checked in
        # TestScenarios) rather than the 3/2/3/3 dev/holdout/holdout2/
        # holdout3 shape the other four families share.
        by_family_split = {}
        for t in bt.TASKS.values():
            if t.family == "scenario":
                self.assertIn(t.split, ("m-dev", "m-holdout3"), t.name)
                continue
            by_family_split.setdefault(t.family, {"dev": 0, "holdout": 0, "holdout2": 0, "holdout3": 0})
            self.assertIn(t.split, ("dev", "holdout", "holdout2", "holdout3"), t.name)
            by_family_split[t.family][t.split] += 1
        for family, counts in by_family_split.items():
            self.assertEqual(counts["dev"], 3, family)
            self.assertEqual(counts["holdout"], 2, family)
            self.assertEqual(counts["holdout2"], 3, family)
            self.assertEqual(counts["holdout3"], 3, family)

    def test_unique_names(self):
        names = [t.name for t in bt.TASKS.values()]
        self.assertEqual(len(names), len(set(names)))
        for name, t in bt.TASKS.items():
            self.assertEqual(name, t.name)

    def test_every_prompt_ends_with_suffix(self):
        for name, t in bt.TASKS.items():
            self.assertTrue(t.prompt.endswith(bt.PROMPT_SUFFIX), name)

    def test_every_files_dict_has_readme(self):
        # Scenario tasks (kind="scenario") namespace every subtask file under
        # t{turn}-{subtask}/ -- a bare "README.md" is never a top-level key
        # for them; see TestScenarios for their own namespaced-file check.
        for name, t in bt.TASKS.items():
            if t.kind == "scenario":
                continue
            self.assertIn("README.md", t.files, name)

    def test_protected_includes_readme_and_public_test_when_present(self):
        for name, t in bt.TASKS.items():
            if t.kind == "scenario":
                continue
            self.assertIn("README.md", t.protected, name)
            if "test_public.py" in t.files:
                self.assertIn("test_public.py", t.protected, name)

    def test_families_and_split_helpers(self):
        self.assertEqual(set(bt.families()), {"repair", "edit", "bugfix", "answer", "scenario"})
        dev = bt.split("dev")
        holdout = bt.split("holdout")
        holdout2 = bt.split("holdout2")
        holdout3 = bt.split("holdout3")
        m_dev = bt.split("m-dev")
        m_holdout3 = bt.split("m-holdout3")
        allnames = bt.split("all")
        self.assertEqual(len(dev), 12)
        self.assertEqual(len(holdout), 8)
        self.assertEqual(len(holdout2), 12)
        self.assertEqual(len(holdout3), 12)
        self.assertEqual(len(m_dev), 3)
        self.assertEqual(len(m_holdout3), 3)
        self.assertEqual(
            set(dev) | set(holdout) | set(holdout2) | set(holdout3) | set(m_dev) | set(m_holdout3),
            set(allnames),
        )
        self.assertEqual(len(allnames), 50)
        with self.assertRaises(ValueError):
            bt.split("nonsense")


class TestCodeTasks(unittest.TestCase):
    """For every 'code' task: unmodified starter fails, reference solution
    passes with failed == 0, and (when shipped) test_public.py passes via
    `python -m unittest` in a subprocess for the reference solution, and
    fails for the unmodified starter (bugfix family)."""

    def test_all_code_tasks(self):
        # holdout2/holdout3 code tasks are covered by
        # tests/test_battery_tasks_holdout2.py and
        # tests/test_battery_tasks_holdout3.py instead: their reference/wrong
        # solutions live only in those test files, never in
        # bt.REFERENCE_SOLUTIONS (so an agent workspace can't read them).
        code_tasks = [t for t in bt.TASKS.values() if t.kind == "code" and t.split not in ("holdout2", "holdout3")]
        self.assertEqual(len(code_tasks), 15)
        for task in code_tasks:
            with self.subTest(task=task.name):
                ws = _materialize(task.files)
                try:
                    result = task.evaluate(ws)
                    self.assertGreater(result["failed"], 0, f"{task.name}: starter should fail evaluate")
                finally:
                    shutil.rmtree(ws, ignore_errors=True)

                overrides = bt.REFERENCE_SOLUTIONS.get(task.name)
                self.assertIsNotNone(overrides, f"{task.name}: missing REFERENCE_SOLUTIONS entry")
                ws2 = _materialize(task.files, overrides)
                try:
                    result2 = task.evaluate(ws2)
                    self.assertEqual(
                        result2["failed"], 0,
                        f"{task.name}: reference solution should pass, got {result2['failure_labels']}",
                    )
                    self.assertGreater(result2["checks"], 0, task.name)

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
                            f"{task.name}: reference solution should pass test_public.py\n{proc.stdout}\n{proc.stderr}",
                        )
                finally:
                    shutil.rmtree(ws2, ignore_errors=True)

    def test_bugfix_starters_fail_public_test(self):
        for task in bt.TASKS.values():
            if task.family != "bugfix":
                continue
            with self.subTest(task=task.name):
                ws = _materialize(task.files)
                try:
                    proc = subprocess.run(
                        [sys.executable, "-m", "unittest", "test_public.py"],
                        cwd=str(ws),
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    self.assertNotEqual(proc.returncode, 0, f"{task.name}: buggy starter should fail public test")
                finally:
                    shutil.rmtree(ws, ignore_errors=True)


class TestAnswerTasks(unittest.TestCase):
    _CORRECT = {
        "answer_audit_log_key": ["AUD-ORDER-7781", "`AUD-ORDER-7781`", "aud-order-7781."],
        "answer_compute_fee": ["480", "`480`", "The fee is 480."],
        "answer_sqlite_import_module": ["storage", "`storage`", "STORAGE"],
        "answer_default_port": ["8765"],
        "answer_exception_count": ["4", "four", "There are 4 distinct exceptions."],
    }
    _DECOYS = {
        "answer_audit_log_key": ["MET-ORDER-7781", "AUD-ORDER-7782", "AUD-order-778"],
        "answer_compute_fee": ["600", "500", "120"],
        "answer_sqlite_import_module": ["decoy_db_helpers", "cache", "network", "utils"],
        "answer_default_port": ["9999", "80", "443"],
        "answer_exception_count": ["3", "5", "2"],
    }

    def test_answer_tasks_are_answer_kind_with_zero_check_evaluate(self):
        for name in self._CORRECT:
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


class TestCheckAnswerMarkdownWrapping(unittest.TestCase):
    """Defect 3: an ANSWER: line wrapped in markdown emphasis, backticks, or a
    blockquote/list marker must still be recognized and scored correctly."""

    def test_bold_wrapped_answer_line(self):
        task = bt.TASKS["answer_default_port"]
        result = bt.check_answer(task, "**ANSWER: 8765**")
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(result["passed"], 1)

    def test_backtick_wrapped_answer_line(self):
        task = bt.TASKS["answer_sqlite_import_module"]
        result = bt.check_answer(task, "`ANSWER: storage`")
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(result["passed"], 1)

    def test_blockquote_marker_and_trailing_period(self):
        task = bt.TASKS["answer_exception_count"]
        result = bt.check_answer(task, "> answer: 4.")
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(result["passed"], 1)

    def test_multiline_message_with_answer_not_on_last_line(self):
        task = bt.TASKS["answer_default_port"]
        message = "ANSWER: 8765\n(post-hoc note appended after the answer line)"
        result = bt.check_answer(task, message)
        self.assertEqual(result["failed"], 0, result)
        self.assertEqual(result["passed"], 1)

    def test_decoy_still_fails_when_markdown_wrapped(self):
        task = bt.TASKS["answer_default_port"]
        result = bt.check_answer(task, "**ANSWER: 9999**")
        self.assertEqual(result["failed"], 1, result)
        self.assertEqual(result["failure_labels"], ["answer_mismatch"])


class TestScenarios(unittest.TestCase):
    """m-dev multi-turn scenarios (scn_dev_1..3): file namespacing, prompt
    construction, and per-turn grading fold -- all without ever invoking
    amplifier (that's forge_e2e's job; see tests/test_forge_e2e_scenario.py).
    """

    SCENARIO_NAMES = ("scn_dev_1", "scn_dev_2", "scn_dev_3")

    def test_scenarios_registered_under_m_dev_with_four_subtasks(self):
        self.assertEqual(set(bt.split("m-dev")), set(self.SCENARIO_NAMES))
        for name in self.SCENARIO_NAMES:
            task = bt.TASKS[name]
            self.assertEqual(task.kind, "scenario")
            self.assertEqual(task.family, "scenario")
            self.assertEqual(task.split, "m-dev")
            self.assertEqual(len(task.subtasks), 4)
            families_in_order = [bt.TASKS[s].family for s in task.subtasks]
            self.assertEqual(families_in_order, ["repair", "answer", "edit", "bugfix"])

    def test_files_and_protected_are_namespaced_per_turn(self):
        for name in self.SCENARIO_NAMES:
            task = bt.TASKS[name]
            with self.subTest(scenario=name):
                for i, subtask_name in enumerate(task.subtasks, start=1):
                    directory = bt.scenario_turn_dir(i, subtask_name)
                    subtask = bt.TASKS[subtask_name]
                    for relpath in subtask.files:
                        self.assertIn(f"{directory}/{relpath}", task.files)
                    for relpath in subtask.protected:
                        self.assertIn(f"{directory}/{relpath}", task.protected)
                # No cross-turn collisions: every scenario file belongs to exactly one turn directory.
                seen_dirs = {p.split("/", 1)[0] for p in task.files}
                expected_dirs = {bt.scenario_turn_dir(i, s) for i, s in enumerate(task.subtasks, start=1)}
                self.assertEqual(seen_dirs, expected_dirs)

    def test_prompts_reference_the_correct_subdirectory_each_turn(self):
        for name in self.SCENARIO_NAMES:
            task = bt.TASKS[name]
            turn_prompts = bt.scenario_turn_prompts(task.subtasks)
            self.assertEqual(len(turn_prompts), 4)
            for i, (subtask_name, turn_prompt) in enumerate(zip(task.subtasks, turn_prompts), start=1):
                directory = bt.scenario_turn_dir(i, subtask_name)
                self.assertTrue(turn_prompt.startswith(f"In the directory {directory}/: "), turn_prompt[:80])
                self.assertIn(bt.TASKS[subtask_name].prompt, turn_prompt)

    def test_registered_prompt_is_exactly_the_joined_turn_prompts(self):
        """The scenario Task's own `.prompt` (what gets prompt_sha256-hashed
        and preregistered) must reconstruct byte-for-byte from `subtasks`
        alone -- this is what lets forge_e2e re-derive turn prompts at run
        time and assert they match the preregistered hash."""
        for name in self.SCENARIO_NAMES:
            task = bt.TASKS[name]
            rebuilt = bt.SCENARIO_TURN_SEPARATOR.join(bt.scenario_turn_prompts(task.subtasks))
            self.assertEqual(rebuilt, task.prompt)

    def _materialize_scenario(self, task):
        d = Path(tempfile.mkdtemp())
        for relpath, content in task.files.items():
            path = d / relpath
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return d

    def test_fully_correct_scenario_passes(self):
        task = bt.TASKS["scn_dev_1"]  # repair_parse_duration, answer_audit_log_key, edit_cli_dry_run, bugfix_paginate_off_by_one
        ws = self._materialize_scenario(task)
        final_messages = {}
        for i, subtask_name in enumerate(task.subtasks, start=1):
            directory = bt.scenario_turn_dir(i, subtask_name)
            if subtask_name in bt.REFERENCE_SOLUTIONS:
                for relpath, content in bt.REFERENCE_SOLUTIONS[subtask_name].items():
                    (ws / directory / relpath).write_text(content, encoding="utf-8")
            subtask = bt.TASKS[subtask_name]
            if subtask.kind == "answer":
                final_messages[i] = f"ANSWER: {subtask.expected_answer}"
        parts = []
        for i, subtask_name in enumerate(task.subtasks, start=1):
            directory = bt.scenario_turn_dir(i, subtask_name)
            q = bt.evaluate_scenario_turn(subtask_name, ws / directory, final_messages.get(i))
            parts.append((directory, q))
        folded = bt.fold_scenario_qualities(parts)
        self.assertEqual(folded["failed"], 0, folded)
        self.assertGreater(folded["checks"], 0)

    def test_partially_wrong_scenario_fails_with_turn_prefixed_label(self):
        """Leave turn 1 (repair_parse_duration) unfixed; everything else
        correct. The fold must fail, and the failing label must be traceable
        to turn 1's own directory."""
        task = bt.TASKS["scn_dev_1"]
        ws = self._materialize_scenario(task)
        final_messages = {}
        for i, subtask_name in enumerate(task.subtasks, start=1):
            if i == 1:
                continue  # leave the repair starter broken
            directory = bt.scenario_turn_dir(i, subtask_name)
            if subtask_name in bt.REFERENCE_SOLUTIONS:
                for relpath, content in bt.REFERENCE_SOLUTIONS[subtask_name].items():
                    (ws / directory / relpath).write_text(content, encoding="utf-8")
            subtask = bt.TASKS[subtask_name]
            if subtask.kind == "answer":
                final_messages[i] = f"ANSWER: {subtask.expected_answer}"
        parts = []
        for i, subtask_name in enumerate(task.subtasks, start=1):
            directory = bt.scenario_turn_dir(i, subtask_name)
            q = bt.evaluate_scenario_turn(subtask_name, ws / directory, final_messages.get(i))
            parts.append((directory, q))
        folded = bt.fold_scenario_qualities(parts)
        self.assertGreater(folded["failed"], 0, folded)
        turn1_dir = bt.scenario_turn_dir(1, task.subtasks[0])
        self.assertTrue(any(label.startswith(f"{turn1_dir}:") for label in folded["failure_labels"]), folded)

    def test_fold_scenario_qualities_sums_and_prefixes(self):
        parts = [
            ("t1-a", {"checks": 3, "passed": 2, "failed": 1, "failure_labels": ["x"]}),
            ("t2-b", {"checks": 1, "passed": 1, "failed": 0, "failure_labels": []}),
        ]
        folded = bt.fold_scenario_qualities(parts)
        self.assertEqual(folded, {"checks": 4, "passed": 3, "failed": 1, "failure_labels": ["t1-a:x"]})


if __name__ == "__main__":
    unittest.main()
