"""Tests for evals/run.py -- the eval-suite driver over battery.py/campaign.py/
battery_report.py. No test invokes a real harness, provider, forge, or the
network: `run.invoke_tool` (the one subprocess seam) is monkeypatched with a
fake that records calls and returns canned JSON, exactly as the battery.py
tests substitute fakes for forge and the amplifier launcher.
"""
from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "evals"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run
import battery
import battery_tasks
import forge_workloads


def _load_test_cells():
    return run.load_cells(REPO_ROOT / "evals" / "cells.yaml")


def _load_test_suites():
    return run.load_suites(REPO_ROOT / "evals" / "suites.yaml")


COMMON = {
    "out_root": "/out", "base_seed": 20260919, "baseline_source": "/baseline",
    "candidate_source": "/candidate", "candidate_sha": "deadbeef", "polyglot_root": "/poly",
}


class CellToArgvTests(unittest.TestCase):
    def setUp(self):
        self.cells = _load_test_cells()
        self.suites = _load_test_suites()

    def test_every_cell_produces_argv_without_error(self):
        for cid in self.cells["cells"]:
            argv = run.cell_to_argv(cid, self.cells, self.suites, "s1", "dev", 1, **COMMON)
            self.assertIn("--experiment", argv)
            self.assertIn(f"{cid}-s1-dev-r1", argv)

    def test_judge_off_cell_emits_fd_override_backend_unavailable(self):
        argv = run.cell_to_argv("effort-only", self.cells, self.suites, "s1", "dev", 1, **COMMON)
        self.assertIn("--fd-override", argv)
        idx = argv.index("--fd-override")
        self.assertIn("backend=unavailable", argv[idx:])
        self.assertNotIn("--fd-backend", argv)

    def test_jev_cell_emits_backend_and_allow_external_state_together(self):
        argv = run.cell_to_argv("judge-jev+effort", self.cells, self.suites, "s1", "dev", 1, **COMMON)
        self.assertIn("--fd-backend", argv)
        self.assertEqual(argv[argv.index("--fd-backend") + 1], "jev")
        self.assertIn("--allow-external-state", argv)

    def test_every_judge_jev_cell_requires_allow_external_state(self):
        """HC05 (2026-09-20c): every jev twin added for the judge head-to-head
        emits --fd-backend jev + --allow-external-state together, same as
        the original judge-jev+effort cell."""
        jev_cells = [cid for cid, cell in self.cells["cells"].items()
                     if (cell.get("fd") or {}).get("backend") == "jev"]
        self.assertGreaterEqual(len(jev_cells), 5)  # judge-jev, +effort, +effort-incumbent, +route, +route-judged, +phasejudged
        for cid in jev_cells:
            argv = run.cell_to_argv(cid, self.cells, self.suites, "s1", "dev", 1, **COMMON)
            self.assertIn("--fd-backend", argv, cid)
            self.assertEqual(argv[argv.index("--fd-backend") + 1], "jev", cid)
            self.assertIn("--allow-external-state", argv, cid)

    def test_escalation_judge_profile_is_single_argv_element(self):
        argv = run.cell_to_argv("judge-local+effort+route-judged", self.cells, self.suites,
                                 "s1", "dev", 1, **COMMON)
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "--fd-override"]
        routing = [o for o in overrides if o.startswith("model_routing=")]
        self.assertEqual(len(routing), 1)
        payload = json.loads(routing[0].split("=", 1)[1])
        self.assertEqual(payload["escalation_judge"], "judge")
        self.assertEqual(payload["escalate_min_probability"], 0.7)

    def test_phase_judge_profile_is_single_argv_element(self):
        argv = run.cell_to_argv("judge-local+effort-phasejudged", self.cells, self.suites,
                                 "s1", "dev", 1, **COMMON)
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "--fd-override"]
        effort = [o for o in overrides if o.startswith("effort_routing=")]
        self.assertEqual(len(effort), 1)
        payload = json.loads(effort[0].split("=", 1)[1])
        self.assertTrue(payload["phase_judge"])

    def test_compact_json_effort_routing_is_single_argv_element(self):
        argv = run.cell_to_argv("judge-local+effort", self.cells, self.suites, "s1", "dev", 1, **COMMON)
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "--fd-override"]
        effort = [o for o in overrides if o.startswith("effort_routing=")]
        self.assertEqual(len(effort), 1)
        payload = json.loads(effort[0].split("=", 1)[1])
        self.assertEqual(payload, {"orient": "medium", "explore": "low", "implement": "high"})

    def test_model_routing_profile_is_single_argv_element(self):
        argv = run.cell_to_argv("judge-local+effort+route", self.cells, self.suites, "s1", "dev", 1, **COMMON)
        overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "--fd-override"]
        routing = [o for o in overrides if o.startswith("model_routing=")]
        self.assertEqual(len(routing), 1)
        payload = json.loads(routing[0].split("=", 1)[1])
        self.assertEqual(payload["start_model"], "claude-sonnet-5")

    def test_every_new_head_to_head_cell_declares_an_anchor(self):
        """HC05 (2026-09-20c): every cell added for the judge head-to-head
        declares anchor_cell (cross-cell comparison is always ratio-to-own-
        anchor, never raw time -- STUDY-DESIGN.md section 4)."""
        new_cells = (
            "judge-jev", "judge-jev+effort-incumbent",
            "judge-local+effort-phasejudged", "judge-jev+effort-phasejudged",
            "judge-jev+effort+route",
            "judge-local+effort+route-judged", "judge-jev+effort+route-judged",
        )
        for cid in new_cells:
            self.assertIn(cid, self.cells["cells"])
            self.assertTrue(self.cells["cells"][cid].get("anchor_cell"), cid)

    def test_phasejudged_cells_declare_secondary_anchor_at_non_judged_twin(self):
        self.assertEqual(
            self.cells["cells"]["judge-local+effort-phasejudged"]["secondary_anchor"],
            "judge-local+effort",
        )
        self.assertEqual(
            self.cells["cells"]["judge-jev+effort-phasejudged"]["secondary_anchor"],
            "judge-jev+effort",
        )

    def test_route_judged_cells_declare_secondary_anchor_at_rules_twin(self):
        self.assertEqual(
            self.cells["cells"]["judge-local+effort+route-judged"]["secondary_anchor"],
            "judge-local+effort+route",
        )
        self.assertEqual(
            self.cells["cells"]["judge-jev+effort+route-judged"]["secondary_anchor"],
            "judge-jev+effort+route",
        )

    def test_route_judged_cells_declare_require_judged_gate(self):
        for cid in ("judge-local+effort+route-judged", "judge-jev+effort+route-judged",
                    "judge-local+effort-phasejudged", "judge-jev+effort-phasejudged"):
            self.assertTrue(self.cells["cells"][cid]["mechanism_gate"].get("require_judged"), cid)

    def test_new_cells_pass_dependency_validation_together(self):
        # anchor_cell / secondary_anchor / requires_cells all reference cells
        # that must also be requested -- exercise the whole new set at once.
        run.validate_cell_dependencies(list(self.cells["cells"].keys()), self.cells)

    def test_every_cell_gets_explicit_bypass_permissions(self):
        for cid in self.cells["cells"]:
            argv = run.cell_to_argv(cid, self.cells, self.suites, "s1", "dev", 1, **COMMON)
            idx = argv.index("--claude-permission-mode")
            self.assertEqual(argv[idx + 1], "bypassPermissions")

    def test_s1_uses_tasks_flag(self):
        argv = run.cell_to_argv("plain", self.cells, self.suites, "s1", "dev", 1, **COMMON)
        self.assertIn("--tasks", argv)
        self.assertEqual(argv[argv.index("--tasks") + 1], "dev")
        self.assertNotIn("--task-source", argv)

    def test_s2_uses_task_source_polyglot_and_split_not_tasks(self):
        argv = run.cell_to_argv("plain", self.cells, self.suites, "s2", "holdout", 1, **COMMON)
        self.assertIn("--task-source", argv)
        self.assertEqual(argv[argv.index("--task-source") + 1], "polyglot")
        self.assertIn("--split", argv)
        self.assertEqual(argv[argv.index("--split") + 1], "holdout")
        self.assertNotIn("--tasks", argv)

    def test_default_cell_emits_amplifier_bundle_foundation(self):
        argv = run.cell_to_argv("plain", self.cells, self.suites, "s1", "dev", 1, **COMMON)
        idx = argv.index("--amplifier-bundle")
        self.assertEqual(argv[idx + 1], "foundation")

    def test_lean_cell_emits_amplifier_bundle_lean(self):
        argv = run.cell_to_argv("judge-local+effort-incumbent-lean", self.cells, self.suites,
                                 "s1", "dev", 1, **COMMON)
        idx = argv.index("--amplifier-bundle")
        self.assertEqual(argv[idx + 1], "lean")

    def test_lean_cells_declare_the_installed_context_weight_pair(self):
        self.assertEqual(self.cells["cells"]["judge-local+effort-incumbent-lean"]["anchor_cell"], "plain-lean")
        self.assertEqual(
            self.cells["cells"]["judge-local+effort-incumbent-lean"]["secondary_anchor"],
            "judge-local+effort-incumbent",
        )
        self.assertEqual(self.cells["cells"]["plain-lean"]["amplifier_bundle"], "lean")


class SeriesLabelTests(unittest.TestCase):
    def setUp(self):
        self.cells = _load_test_cells()

    def test_label_names_all_four_axes(self):
        cell = self.cells["cells"]["judge-local+effort"]
        label = run.series_label("judge-local+effort", 1, "amplifier-fd", cell,
                                  self.cells["defaults"], self.cells["effort_profiles"],
                                  self.cells["model_routing_profiles"])
        self.assertIn("judge=", label)
        self.assertIn("effort ", label)
        self.assertIn("model routing:", label)
        self.assertIn("model=", label)

    def test_plain_cell_label_shows_everything_off(self):
        cell = self.cells["cells"]["plain"]
        label = run.series_label("plain", 2, "amplifier-plain", cell, self.cells["defaults"],
                                  self.cells["effort_profiles"], self.cells["model_routing_profiles"])
        self.assertIn("judge=off", label)
        self.assertIn("effort off", label)
        self.assertIn("model routing: off", label)

    def test_label_missing_an_axis_is_rejected(self):
        with self.assertRaises(run.EvalsError) as ctx:
            run.validate_series_label("plain r1 amplifier-plain [judge=off; model=x]")
        self.assertEqual(ctx.exception.code, 4)

    def test_cross_check_agrees_on_judge_effort_routing(self):
        declared = ("judge-local+effort r1 amplifier-fd [judge=ollama qwen3:0.6b; "
                    "effort orient->medium, explore->low, implement->high; model routing: off; "
                    "model=claude-fable-5-1]")
        recorded = ("amplifier-fd [judge=ollama qwen3:0.6b; "
                    "effort orient->medium, explore->low, implement->high; model routing: off]")
        self.assertTrue(run.cross_check_series_label(declared, recorded))

    def test_cross_check_disagreement_raises_exit_4(self):
        declared = "x r1 amplifier-fd [judge=ollama qwen3:0.6b; effort off; model routing: off; model=m]"
        recorded = "amplifier-fd [judge=jev jev-remote; effort off; model routing: off]"
        with self.assertRaises(run.EvalsError) as ctx:
            run.cross_check_series_label(declared, recorded)
        self.assertEqual(ctx.exception.code, 4)

    def test_label_includes_bundle_axis(self):
        cell = self.cells["cells"]["judge-local+effort-incumbent-lean"]
        label = run.series_label("judge-local+effort-incumbent-lean", 1, "amplifier-fd", cell,
                                  self.cells["defaults"], self.cells["effort_profiles"],
                                  self.cells["model_routing_profiles"])
        self.assertIn("bundle=lean", label)

    def test_default_cell_label_shows_bundle_foundation(self):
        cell = self.cells["cells"]["judge-local+effort"]
        label = run.series_label("judge-local+effort", 1, "amplifier-fd", cell,
                                  self.cells["defaults"], self.cells["effort_profiles"],
                                  self.cells["model_routing_profiles"])
        self.assertIn("bundle=foundation", label)

    def test_cross_check_ignores_bundle_axis_when_only_recorded_has_it(self):
        """battery.py's recorded label always carries a bundle axis (derived from the
        run's own profile.md); the declared label (run.series_label) also carries one.
        cross_check must still agree on judge/effort/routing when bundle values differ
        in position (declared has model= before bundle=, recorded has none)."""
        declared = ("judge-local+effort-incumbent-lean r1 amplifier-fd [judge=ollama qwen3:0.6b; "
                    "effort explore->low; model routing: off; model=claude-fable-5-1; bundle=lean]")
        recorded = "amplifier-fd [judge=ollama qwen3:0.6b; effort explore->low; model routing: off; bundle=lean]"
        self.assertTrue(run.cross_check_series_label(declared, recorded))


class VerifyPromptsTests(unittest.TestCase):
    def test_passes_on_matched_hashes(self):
        result = run.verify_prompts(
            tasks=["t1"], resolve_task=lambda n: SimpleNamespace(prompt="hello"),
            get_task_prompt=lambda n: "hello",
            amplifier_prompts={"t1": {"amplifier-plain": "hello", "amplifier-fd": "hello"}},
            external_harnesses={"t1": ["claude"]},
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["tasks"]["t1"]["ok"])
        self.assertEqual(result["tasks"]["t1"]["harnesses"]["claude"]["source"], "static-fallback")

    def test_fails_on_mismatched_amplifier_prompt(self):
        result = run.verify_prompts(
            tasks=["t1"], resolve_task=lambda n: SimpleNamespace(prompt="hello"),
            get_task_prompt=lambda n: "hello",
            amplifier_prompts={"t1": {"amplifier-fd": "goodbye"}},
            external_harnesses={},
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["tasks"]["t1"]["harnesses"]["amplifier-fd"]["matched"])

    def test_fails_on_unresolvable_task(self):
        result = run.verify_prompts(
            tasks=["missing"], resolve_task=lambda n: None, get_task_prompt=lambda t: None,
            amplifier_prompts={}, external_harnesses={},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["tasks"]["missing"]["reason"], "unresolvable task")

    def test_fails_on_empty_prompt(self):
        result = run.verify_prompts(
            tasks=["t1"], resolve_task=lambda n: SimpleNamespace(prompt=""), get_task_prompt=lambda t: None,
            amplifier_prompts={}, external_harnesses={},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["tasks"]["t1"]["reason"], "empty/unresolvable prompt")


class GateEvalTests(unittest.TestCase):
    def test_jev_configured_but_never_scored_fails(self):
        gate = run.gate_eval({"scored_backend": "jev", "min_scored": 1},
                              {"mechanism_engaged": False, "mechanism_reason": "external backend 'jev' refused",
                               "scored_by_backend": {}, "fallback_count": 9, "routed_by_route": {},
                               "effort_routed_by_phase_effort": {}, "model_routed_requested_models": {},
                               "model_routed_escalations_by_reason": {}})
        self.assertFalse(gate["passed"])

    def test_judge_off_with_scores_fails(self):
        gate = run.gate_eval({"max_scored": 0},
                              {"mechanism_engaged": True, "mechanism_reason": None,
                               "scored_by_backend": {"unavailable": 1}, "fallback_count": 0,
                               "routed_by_route": {}, "effort_routed_by_phase_effort": {},
                               "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {}})
        self.assertFalse(gate["passed"])

    def test_effort_phase_missing_fails(self):
        gate = run.gate_eval({"require_effort_phases": ["implement:high"]},
                              {"mechanism_engaged": True, "mechanism_reason": None,
                               "scored_by_backend": {}, "fallback_count": 0, "routed_by_route": {},
                               "effort_routed_by_phase_effort": {}, "model_routed_requested_models": {},
                               "model_routed_escalations_by_reason": {}})
        self.assertFalse(gate["passed"])

    def test_zero_escalations_flags_but_does_not_fail(self):
        gate = run.gate_eval({"model_routed_model": "claude-sonnet-5", "min_model_routed": 1,
                               "flag_if_zero_escalations": "confounded_with_plain_sonnet"},
                              {"mechanism_engaged": True, "mechanism_reason": None,
                               "scored_by_backend": {}, "fallback_count": 0, "routed_by_route": {},
                               "effort_routed_by_phase_effort": {},
                               "model_routed_requested_models": {"claude-sonnet-5": 3},
                               "model_routed_escalations_by_reason": {}})
        self.assertTrue(gate["passed"])
        self.assertIn("confounded_with_plain_sonnet", gate["flags"])

    def test_mechanism_engaged_false_fails_regardless_of_other_keys(self):
        gate = run.gate_eval({"scored_backend": "ollama", "min_scored": 1},
                              {"mechanism_engaged": False, "mechanism_reason": "boom",
                               "scored_by_backend": {"ollama": 5}, "fallback_count": 0,
                               "routed_by_route": {}, "effort_routed_by_phase_effort": {},
                               "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {}})
        self.assertFalse(gate["passed"])

    def test_no_fd_receipts_true_when_mechanism_is_none(self):
        gate = run.gate_eval({"no_fd_receipts": True}, None)
        self.assertTrue(gate["passed"])

    def test_no_fd_receipts_fails_when_receipts_leaked(self):
        gate = run.gate_eval({"no_fd_receipts": True},
                              {"mechanism_engaged": True, "mechanism_reason": None,
                               "scored_by_backend": {"ollama": 1}, "fallback_count": 0,
                               "routed_by_route": {}, "effort_routed_by_phase_effort": {},
                               "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {}})
        self.assertFalse(gate["passed"])

    def test_require_judged_false_fails_gate_with_judged_reason(self):
        gate = run.gate_eval(
            {"scored_backend": "ollama", "min_scored": 1, "require_judged": True},
            {"mechanism_engaged": True, "mechanism_reason": None,
             "scored_by_backend": {"ollama": 5}, "fallback_count": 0,
             "routed_by_route": {}, "effort_routed_by_phase_effort": {},
             "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
             "judged_engaged": False,
             "judged_reason": "phase_judge configured but 0 phase_judged receipts",
             "latency_within_budget": None},
        )
        self.assertFalse(gate["passed"])
        self.assertEqual(gate["reason"], "phase_judge configured but 0 phase_judged receipts")

    def test_require_judged_true_passes_gate_and_exposes_latency(self):
        gate = run.gate_eval(
            {"scored_backend": "ollama", "min_scored": 1, "require_judged": True},
            {"mechanism_engaged": True, "mechanism_reason": None,
             "scored_by_backend": {"ollama": 5}, "fallback_count": 0,
             "routed_by_route": {}, "effort_routed_by_phase_effort": {},
             "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
             "judged_engaged": True, "judged_reason": None,
             "latency_within_budget": True},
        )
        self.assertTrue(gate["passed"])
        self.assertIsNone(gate["reason"])
        self.assertTrue(gate["latency_within_budget"])

    def test_require_judged_absent_leaves_non_judged_cell_unaffected(self):
        gate = run.gate_eval(
            {"scored_backend": "ollama", "min_scored": 1},
            {"mechanism_engaged": True, "mechanism_reason": None,
             "scored_by_backend": {"ollama": 5}, "fallback_count": 0,
             "routed_by_route": {}, "effort_routed_by_phase_effort": {},
             "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
             "judged_engaged": None, "judged_reason": None,
             "latency_within_budget": None},
        )
        self.assertTrue(gate["passed"])
        self.assertIsNone(gate["latency_within_budget"])


class ResumeDetectionTests(unittest.TestCase):
    def test_no_proposal_means_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            action, reason = run.resume_detection(Path(tmp) / "exp1", ["--seed", "1"])
            self.assertEqual(action, "prepare")
            self.assertIsNone(reason)

    def test_matching_recorded_argv_reuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "exp1"
            d.mkdir()
            (d / "proposal.json").write_text("{}")
            (d / "run-argv.json").write_text(json.dumps(["--seed", "1"]))
            action, reason = run.resume_detection(d, ["--seed", "1"])
            self.assertEqual(action, "reuse")

    def test_changed_cell_definition_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "exp1"
            d.mkdir()
            (d / "proposal.json").write_text("{}")
            (d / "run-argv.json").write_text(json.dumps(["--seed", "1"]))
            action, error_reason = run.resume_detection(d, ["--seed", "2"])
            self.assertEqual(action, "error")
            self.assertIn("changed", error_reason)


class ExitCodeTests(unittest.TestCase):
    def _run(self, argv, calls):
        def fake_invoke(tool, targv):
            calls.append((tool, targv))
            raise AssertionError("no invoke_tool call should happen before a pre-launch failure")

        orig = run.invoke_tool
        run.invoke_tool = fake_invoke
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                return run.main(argv + ["--out", out])
        finally:
            run.invoke_tool = orig

    def test_unknown_cell_exits_2_with_zero_launches(self):
        calls = []
        rc = self._run(["--suite", "s1", "--split", "dev", "--cells", "not-a-cell",
                         "--candidate-sha", "deadbeef", "--baseline-source", "/b"], calls)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_routing_cell_without_control_exits_2_with_zero_launches(self):
        calls = []
        rc = self._run(["--suite", "s1", "--split", "dev", "--cells", "judge-local+effort+route",
                         "--candidate-sha", "deadbeef", "--baseline-source", "/b"], calls)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_jev_cell_without_consent_flag_exits_2_with_zero_launches(self):
        calls = []
        rc = self._run(["--suite", "s1", "--split", "dev", "--cells", "judge-jev+effort",
                         "--candidate-sha", "deadbeef", "--baseline-source", "/b"], calls)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_holdout_without_preregistration_exits_2_with_zero_launches(self):
        calls = []
        rc = self._run(["--suite", "s1", "--split", "holdout", "--cells", "plain",
                         "--candidate-sha", "deadbeef", "--baseline-source", "/b"], calls)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_holdout2_without_preregistration_exits_2_with_zero_launches(self):
        calls = []
        rc = self._run(["--suite", "s1", "--split", "holdout2", "--cells", "plain",
                         "--candidate-sha", "deadbeef", "--baseline-source", "/b"], calls)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])

    def test_holdout_split_helper(self):
        for name in ("holdout", "holdout2", "holdout3", "m-holdout3"):
            self.assertTrue(run.is_holdout_split(name), name)
        for name in ("dev", "m-dev", "", None):
            self.assertFalse(run.is_holdout_split(name), name)

    def test_missing_candidate_sha_exits_2_with_zero_launches(self):
        calls = []
        rc = self._run(["--suite", "s1", "--split", "dev", "--cells", "plain",
                         "--baseline-source", "/b"], calls)
        self.assertEqual(rc, 2)
        self.assertEqual(calls, [])


class CostEstimateTests(unittest.TestCase):
    def test_deterministic_estimate_from_runs_times_per_launch(self):
        cells = _load_test_cells()
        est = run.cost_estimate(3, 2, cells)
        self.assertEqual(est["experiments"], 6)
        self.assertEqual(est["estimated_usd"], 6 * cells["budget"]["per_launch_usd"])

    def test_dry_run_touches_no_filesystem_state_outside_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out")
            rc = run.main(["--suite", "s1", "--split", "dev", "--cells", "plain", "--reps", "1",
                           "--out", out, "--baseline-source", "/b", "--candidate-sha", "deadbeef",
                           "--dry-run"])
            self.assertEqual(rc, 0)
            self.assertFalse(Path(out).exists())


class ManifestShapeTests(unittest.TestCase):
    def test_required_keys_present(self):
        manifest = run.build_manifest(
            argv=["x"], suite_id="s1", split="dev", reps=1, suite_doc={},
            candidate={"source": "s"}, baseline={"source": "b"}, cells_report=[],
            budget_report={}, tool_shas={"battery.py": "abc"},
        )
        run.validate_manifest_shape(manifest)  # must not raise

    def test_missing_key_raises_exit_4(self):
        manifest = {"schema": run.SCHEMA_MANIFEST}
        with self.assertRaises(run.EvalsError) as ctx:
            run.validate_manifest_shape(manifest)
        self.assertEqual(ctx.exception.code, 4)

    def test_changed_tool_sha_on_resume_exits_4(self):
        with self.assertRaises(run.EvalsError) as ctx:
            run.verify_tool_shas({"battery.py": "old"}, {"battery.py": "new"})
        self.assertEqual(ctx.exception.code, 4)

    def test_matching_tool_shas_do_not_raise(self):
        self.assertTrue(run.verify_tool_shas({"battery.py": "same"}, {"battery.py": "same"}))


class EndToEndTwoCellTests(unittest.TestCase):
    """Two-cell, one-rep, drives init -> prepare -> verify -> run -> reevaluate ->
    evaluate -> gate -> report with a fake `invoke_tool`, asserting call order and
    that runs never overlap (serial)."""

    def test_full_sequence_with_fakes(self):
        calls = []

        def fake_invoke(tool, argv):
            calls.append((tool, list(argv)))
            if tool == "campaign" and argv[0] == "init":
                root = Path(argv[argv.index("--root") + 1])
                root.mkdir(parents=True, exist_ok=True)
                (root / "protocol.json").write_text("{}")
                return {"initialized": str(root)}
            if tool == "campaign" and argv[:2] == ["budget", "status"]:
                return {"remaining": 1000.0}
            if tool == "battery" and argv[0] == "prepare":
                flags = {}
                it = iter(argv[1:])
                for a in it:
                    if a.startswith("--"):
                        flags[a] = next(it, True)
                exp_dir = battery.experiment_dir_for(Path(flags["--root"]), flags["--experiment"])
                exp_dir.mkdir(parents=True, exist_ok=True)
                (exp_dir / "proposal.json").write_text(json.dumps({
                    "tasks": ["t1"], "claude_permission_mode": "bypassPermissions",
                    "commands": {}, "task_source": None, "candidate_source_snapshot": None,
                    "frozen_run_schedule": [],
                }))
                return {"prepared": str(exp_dir)}
            if tool == "battery" and argv[0] == "run":
                return {"experiment": argv[argv.index("--experiment") + 1]}
            if tool == "battery" and argv[0] == "reevaluate":
                return {"experiment": argv[argv.index("--experiment") + 1], "changed": []}
            if tool == "battery" and argv[0] == "evaluate":
                exp = argv[argv.index("--experiment") + 1]
                if exp.startswith("plain-"):
                    mechanism = None
                else:
                    mechanism = {
                        "mechanism_engaged": True, "mechanism_reason": None,
                        "scored_by_backend": {"ollama": 1}, "fallback_count": 0,
                        "routed_by_route": {}, "effort_routed_by_phase_effort": {"implement:high": 1},
                        "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
                    }
                return {"experiment": exp, "mechanism": mechanism, "amplifier_fd_series_label": None}
            if tool == "battery_report":
                return {"report": str(Path(argv[argv.index("--out") + 1]))}
            raise AssertionError(f"unexpected invoke_tool call: {tool} {argv}")

        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = fake_invoke
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain,judge-local+effort",
                    "--reps", "1", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi",
                    "--events-dir", "/ev",
                ])
                self.assertEqual(rc, 0)
                manifest = json.loads((Path(out) / "manifest.json").read_text())
                self.assertEqual(manifest["schema"], run.SCHEMA_MANIFEST)
                self.assertTrue((Path(out) / "gates.json").exists())
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify

        tool_order = [c[0] for c in calls]
        self.assertEqual(tool_order[0], "campaign")  # init first
        # every prepare precedes its own run/reevaluate/evaluate, no interleave of two runs
        run_indices = [i for i, c in enumerate(calls) if c[0] == "battery" and c[1][0] == "run"]
        self.assertEqual(len(run_indices), 2)


class AggregateTaskPairsTests(unittest.TestCase):
    def test_cross_style_comparison(self):
        comp = {"cross": {"baselines": {"amplifier-plain": {"per_task": {
            "t1": {"candidate_exec_s": 8.0, "candidate_passed": True,
                   "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True},
        }}}}}
        pairs = run.aggregate_task_pairs([comp])
        self.assertEqual(pairs["t1"]["candidate_exec_s"], [8.0])
        self.assertEqual(pairs["t1"]["anchor_exec_s"], [10.0])
        self.assertEqual(pairs["t1"]["candidate_passed"], [True])

    def test_per_task_style_comparison_no_cross(self):
        comp = {"per_task": {"t1": {
            "amplifier-fd": {"penalized_ms": 8000.0, "outcome_passed": True},
            "amplifier-plain": {"penalized_ms": 10000.0, "outcome_passed": True},
        }}}
        pairs = run.aggregate_task_pairs([comp])
        self.assertAlmostEqual(pairs["t1"]["candidate_exec_s"][0], 8.0)
        self.assertAlmostEqual(pairs["t1"]["anchor_exec_s"][0], 10.0)

    def test_missing_comparison_entries_are_skipped(self):
        pairs = run.aggregate_task_pairs([None, {}])
        self.assertEqual(pairs, {})

    def test_rep_numbers_track_position_including_none_gaps(self):
        comp1 = {"cross": {"baselines": {"amplifier-plain": {"per_task": {
            "t1": {"candidate_exec_s": 8.0, "candidate_passed": True,
                   "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True},
        }}}}}
        comp3 = {"cross": {"baselines": {"amplifier-plain": {"per_task": {
            "t1": {"candidate_exec_s": 7.0, "candidate_passed": False,
                   "amplifier-plain_exec_s": 9.0, "amplifier-plain_passed": True},
        }}}}}
        # rep 2 is None (e.g. an infra failure); rep numbers must still read 1 and 3.
        pairs = run.aggregate_task_pairs([comp1, None, comp3])
        self.assertEqual(pairs["t1"]["rep"], [1, 3])


class PerTaskLogRatiosAndQualityTests(unittest.TestCase):
    def test_median_across_reps_then_log_ratio(self):
        task_pairs = {"t1": {
            "candidate_exec_s": [8.0, 10.0, None], "candidate_passed": [True, True, False],
            "anchor_exec_s": [10.0, 12.0, 9.0], "anchor_passed": [True, True, True],
        }}
        ratios = run.per_task_log_ratios(task_pairs)
        # only reps where BOTH passed count: (8,10) and (10,12); medians 9.0/11.0
        self.assertAlmostEqual(ratios["t1"], math.log(9.0 / 11.0))

    def test_task_with_no_paired_passing_rep_excluded(self):
        task_pairs = {"t1": {
            "candidate_exec_s": [8.0], "candidate_passed": [False],
            "anchor_exec_s": [10.0], "anchor_passed": [True],
        }}
        self.assertEqual(run.per_task_log_ratios(task_pairs), {})

    def test_quality_counts_majority_vote(self):
        task_pairs = {
            "t1": {"candidate_passed": [True, True, False], "anchor_passed": [True, False, False],
                   "candidate_exec_s": [], "anchor_exec_s": []},
            "t2": {"candidate_passed": [False, False], "anchor_passed": [True, True],
                   "candidate_exec_s": [], "anchor_exec_s": []},
        }
        cand, anchor, n = run.quality_counts(task_pairs)
        self.assertEqual(cand, 1)   # t1 majority pass, t2 majority fail
        self.assertEqual(anchor, 1)  # t1 majority fail, t2 majority pass
        self.assertEqual(n, 2)


class OutcomeRegressionFailuresTests(unittest.TestCase):
    def test_flags_candidate_failed_where_anchor_passed(self):
        task_pairs = {
            "repair_roman_to_int": {
                "candidate_passed": [True, False, False], "anchor_passed": [True, True, True],
                "candidate_exec_s": [], "anchor_exec_s": [], "rep": [1, 2, 3],
            },
        }
        failures = run.outcome_regression_failures(task_pairs)
        self.assertEqual(len(failures), 2)
        self.assertEqual([f["rep"] for f in failures], [2, 3])
        for f in failures:
            self.assertEqual(f["task"], "repair_roman_to_int")
            self.assertEqual(f["labels"], ["candidate_failed_where_anchor_passed"])

    def test_no_failure_when_both_pass_or_both_fail_or_candidate_alone_passes(self):
        task_pairs = {
            "t1": {"candidate_passed": [True, False], "anchor_passed": [True, False],
                   "candidate_exec_s": [], "anchor_exec_s": [], "rep": [1, 2]},
            "t2": {"candidate_passed": [True], "anchor_passed": [False],
                   "candidate_exec_s": [], "anchor_exec_s": [], "rep": [1]},
        }
        self.assertEqual(run.outcome_regression_failures(task_pairs), [])

    def test_falls_back_to_positional_rep_numbers_when_rep_key_absent(self):
        task_pairs = {"t1": {"candidate_passed": [False], "anchor_passed": [True],
                              "candidate_exec_s": [], "anchor_exec_s": []}}
        failures = run.outcome_regression_failures(task_pairs)
        self.assertEqual(failures, [{"task": "t1", "rep": 1, "labels": ["candidate_failed_where_anchor_passed"]}])


class ProtectedFileAndCrashFailuresTests(unittest.TestCase):
    def test_empty_without_campaign_root_or_experiments(self):
        self.assertEqual(run.protected_file_and_crash_failures(None, ["exp-r1"]), [])
        self.assertEqual(run.protected_file_and_crash_failures("/campaign", None), [])

    def test_detects_protected_file_violation_and_evaluator_crash(self):
        assigned = {
            ("repair_roman_to_int", "amplifier-fd"): {"result": {
                "protected_files_unchanged": {"protected.py": False, "readme.md": True},
                "quality": {"failure_labels": []},
            }},
            ("other_task", "amplifier-fd"): {"result": {
                "protected_files_unchanged": {"protected.py": True},
                "quality": {"failure_labels": ["evaluate_error:boom"]},
            }},
            ("plain_task", "amplifier-plain"): {"result": {  # wrong harness, ignored
                "protected_files_unchanged": {"protected.py": False},
            }},
        }
        fake_battery = SimpleNamespace(
            experiment_dir_for=lambda root, exp: Path(root) / exp,
            _load_experiment_assigned=lambda experiment_dir: ({}, assigned),
        )
        with patch.dict(sys.modules, {"battery": fake_battery}):
            failures = run.protected_file_and_crash_failures("/campaign", ["exp-r1"])
        by_task = {f["task"]: f for f in failures if f["labels"][0].startswith("protected_file_modified")}
        self.assertIn("protected_file_modified:protected.py", by_task["repair_roman_to_int"]["labels"])
        crash = next(f for f in failures if f["task"] == "other_task" and "evaluator_crash" in f["labels"][0])
        self.assertEqual(crash["rep"], 1)
        self.assertIn("evaluator_crash:evaluate_error:boom", crash["labels"])

    def test_ordinary_task_check_failure_label_is_not_mistaken_for_a_crash(self):
        # battery_tasks.py's own check-failure labels (e.g. "<case-id>:expected-
        # error-got-ok") contain the substring "error" but are NOT an evaluator
        # crash -- only the two literal exception-wrapper prefixes battery.py's
        # _evaluate_quality actually raises under (or the timeout sentinel) count.
        assigned = {("repair_roman_to_int", "amplifier-fd"): {"result": {
            "protected_files_unchanged": {},
            "quality": {"failure_labels": ["rand-4:expected-error-got-ok", "answer_mismatch"]},
        }}}
        fake_battery = SimpleNamespace(
            experiment_dir_for=lambda root, exp: Path(root) / exp,
            _load_experiment_assigned=lambda experiment_dir: ({}, assigned),
        )
        with patch.dict(sys.modules, {"battery": fake_battery}):
            failures = run.protected_file_and_crash_failures("/campaign", ["exp-r1"])
        self.assertEqual(failures, [])

    def test_unloadable_experiment_is_skipped_not_raised(self):
        def _boom(experiment_dir):
            raise FileNotFoundError("no manifest")
        fake_battery = SimpleNamespace(
            experiment_dir_for=lambda root, exp: Path(root) / exp,
            _load_experiment_assigned=_boom,
        )
        with patch.dict(sys.modules, {"battery": fake_battery}):
            failures = run.protected_file_and_crash_failures("/campaign", ["exp-r1", None])
        self.assertEqual(failures, [])


class BootstrapCiTests(unittest.TestCase):
    def test_empty_returns_none_triplet(self):
        self.assertEqual(run.bootstrap_ci_log_ratio({}), (None, None, None))

    def test_identical_ratios_collapse_ci_to_point(self):
        log_ratios = {"t1": math.log(0.8), "t2": math.log(0.8), "t3": math.log(0.8)}
        point, lo, hi = run.bootstrap_ci_log_ratio(log_ratios)
        self.assertAlmostEqual(point, 0.8)
        self.assertAlmostEqual(lo, 0.8)
        self.assertAlmostEqual(hi, 0.8)

    def test_deterministic_with_fixed_seed(self):
        log_ratios = {"t1": math.log(0.7), "t2": math.log(1.3), "t3": math.log(0.9), "t4": math.log(1.1)}
        first = run.bootstrap_ci_log_ratio(log_ratios, seed=42)
        second = run.bootstrap_ci_log_ratio(log_ratios, seed=42)
        self.assertEqual(first, second)

    def test_ci_bounds_bracket_the_point_for_mixed_data(self):
        log_ratios = {"t1": math.log(0.5), "t2": math.log(2.0), "t3": math.log(0.6), "t4": math.log(1.8)}
        point, lo, hi = run.bootstrap_ci_log_ratio(log_ratios)
        self.assertLessEqual(lo, point)
        self.assertGreaterEqual(hi, point)


class SignTestAndCostRatioTests(unittest.TestCase):
    def test_sign_test_p_from_log_ratios_all_negative(self):
        log_ratios = {"t1": -0.1, "t2": -0.2}
        p = run.sign_test_p_from_log_ratios(log_ratios)
        self.assertAlmostEqual(p, 0.5)

    def test_cost_ratio_from_per_harness(self):
        candidate = [{"per_harness": {"amplifier-fd": {"mean_cost_known_usd": 0.5, "unknown_cost_count": 0}}}]
        anchor = [{"per_harness": {"amplifier-plain": {"mean_cost_known_usd": 1.0, "unknown_cost_count": 1}}}]
        ratio, unknown = run.cost_ratio_from_cell_comparisons(candidate, anchor)
        self.assertAlmostEqual(ratio, 0.5)
        self.assertEqual(unknown, 1)

    def test_cost_ratio_none_when_unavailable(self):
        ratio, unknown = run.cost_ratio_from_cell_comparisons([{"per_harness": {}}], [{"per_harness": {}}])
        self.assertIsNone(ratio)
        self.assertEqual(unknown, 0)


class ClassifyVerdictTests(unittest.TestCase):
    BASE = dict(reps=3, split="holdout", gate_passed=True, quality_non_inferior=True,
                ratio_point=0.85, ratio_ci_low=0.75, ratio_ci_high=0.95, sign_p=0.01,
                paired_task_count=10, cost_ratio=0.9)

    def test_gate_failed_takes_priority(self):
        kwargs = {**self.BASE, "gate_passed": False}
        self.assertEqual(run.classify_verdict(**kwargs), "gate-failed")

    def test_quality_regressed(self):
        kwargs = {**self.BASE, "quality_non_inferior": False}
        self.assertEqual(run.classify_verdict(**kwargs), "quality-regressed")

    def test_confirmed_on_holdout_with_full_bar(self):
        self.assertEqual(run.classify_verdict(**self.BASE), "confirmed")

    def test_screen_on_dev_split_even_with_good_numbers(self):
        kwargs = {**self.BASE, "split": "dev"}
        self.assertEqual(run.classify_verdict(**kwargs), "screen")

    def test_screen_when_ci_includes_one(self):
        kwargs = {**self.BASE, "ratio_ci_high": 1.05}
        self.assertEqual(run.classify_verdict(**kwargs), "screen")

    def test_screen_when_fewer_than_3_reps(self):
        kwargs = {**self.BASE, "reps": 1}
        self.assertEqual(run.classify_verdict(**kwargs), "screen")

    def test_no_effect_when_ratio_near_one_with_enough_evidence(self):
        kwargs = {**self.BASE, "ratio_point": 1.0, "ratio_ci_high": 1.05, "sign_p": 0.8}
        self.assertEqual(run.classify_verdict(**kwargs), "no-effect")

    def test_confirmed_on_holdout2_split(self):
        # holdout2 is a fresh holdout split (evals/suites.yaml) -- it must be
        # eligible for "confirmed" exactly like "holdout", never like "dev".
        kwargs = {**self.BASE, "split": "holdout2"}
        self.assertEqual(run.classify_verdict(**kwargs), "confirmed")

    def test_dev_split_never_confirmed_even_named_like_holdout2(self):
        kwargs = {**self.BASE, "split": "dev"}
        self.assertEqual(run.classify_verdict(**kwargs), "screen")

    def test_m_dev_split_never_confirmed(self):
        kwargs = {**self.BASE, "split": "m-dev"}
        self.assertEqual(run.classify_verdict(**kwargs), "screen")

    def test_critical_failure_disqualifies_even_with_full_bar(self):
        kwargs = {**self.BASE, "critical_failure_count": 1}
        self.assertEqual(run.classify_verdict(**kwargs), "disqualified (critical failure)")

    def test_critical_failure_is_never_merely_quality_regressed(self):
        kwargs = {**self.BASE, "critical_failure_count": 2, "quality_non_inferior": False}
        self.assertEqual(run.classify_verdict(**kwargs), "disqualified (critical failure)")

    def test_gate_failed_still_outranks_critical_failure(self):
        kwargs = {**self.BASE, "critical_failure_count": 1, "gate_passed": False}
        self.assertEqual(run.classify_verdict(**kwargs), "gate-failed")


class BuildCellResultTests(unittest.TestCase):
    def test_end_to_end_confirmed(self):
        comparisons = [{"cross": {"baselines": {"amplifier-plain": {"per_task": {
            f"t{i}": {"candidate_exec_s": 8.0, "candidate_passed": True,
                      "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True}
            for i in range(10)
        }}}}, "per_harness": {"amplifier-fd": {"mean_cost_known_usd": 0.4, "unknown_cost_count": 0}}}] * 3
        anchor_comparisons = [{"per_harness": {"amplifier-plain": {"mean_cost_known_usd": 0.5,
                                                                    "unknown_cost_count": 0}}}] * 3
        row = run.build_cell_result("judge-local+effort", "plain", comparisons, anchor_comparisons,
                                     True, "holdout", 3)
        self.assertEqual(row["verdict"], "confirmed")
        self.assertAlmostEqual(row["exec_time_ratio"]["geomean"], 0.8)
        self.assertAlmostEqual(row["cost_ratio"], 0.8)
        self.assertEqual(row["quality"]["candidate_successes"], 10)
        self.assertEqual(row["critical_failures"], [])

    def test_end_to_end_disqualified_when_candidate_fails_where_anchor_passed(self):
        # Same shape as test_end_to_end_confirmed, but on rep 2 one task's
        # candidate fails while the anchor still passes -- STUDY-DESIGN.md
        # section 8's third critical-failure kind. Must never be "confirmed".
        def _per_task(rep):
            table = {
                f"t{i}": {"candidate_exec_s": 8.0, "candidate_passed": True,
                          "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True}
                for i in range(10)
            }
            if rep == 2:
                table["t0"] = {"candidate_exec_s": None, "candidate_passed": False,
                               "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True}
            return table

        comparisons = [
            {"cross": {"baselines": {"amplifier-plain": {"per_task": _per_task(rep)}}},
             "per_harness": {"amplifier-fd": {"mean_cost_known_usd": 0.4, "unknown_cost_count": 0}}}
            for rep in (1, 2, 3)
        ]
        anchor_comparisons = [{"per_harness": {"amplifier-plain": {"mean_cost_known_usd": 0.5,
                                                                    "unknown_cost_count": 0}}}] * 3
        row = run.build_cell_result("judge-local+effort", "plain", comparisons, anchor_comparisons,
                                     True, "holdout", 3)
        self.assertEqual(row["verdict"], "disqualified (critical failure)")
        self.assertEqual(len(row["critical_failures"]), 1)
        self.assertEqual(row["critical_failures"][0]["task"], "t0")
        self.assertEqual(row["critical_failures"][0]["rep"], 2)

    def test_protected_file_and_crash_failures_feed_into_verdict(self):
        comparisons = [{"cross": {"baselines": {"amplifier-plain": {"per_task": {
            f"t{i}": {"candidate_exec_s": 8.0, "candidate_passed": True,
                      "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True}
            for i in range(10)
        }}}}, "per_harness": {"amplifier-fd": {"mean_cost_known_usd": 0.4, "unknown_cost_count": 0}}}] * 3
        anchor_comparisons = [{"per_harness": {"amplifier-plain": {"mean_cost_known_usd": 0.5,
                                                                    "unknown_cost_count": 0}}}] * 3
        assigned = {("t0", "amplifier-fd"): {"result": {
            "protected_files_unchanged": {"protected.py": False},
            "quality": {"failure_labels": []},
        }}}
        with (
            patch.object(battery, "experiment_dir_for", lambda root, exp: Path(root) / exp),
            patch.object(battery, "_load_experiment_assigned", lambda experiment_dir: ({}, assigned)),
        ):
            row = run.build_cell_result(
                "judge-local+effort", "plain", comparisons, anchor_comparisons, True, "holdout", 3,
                campaign_root="/campaign", candidate_experiments=["exp-r1", "exp-r2", "exp-r3"],
            )
        self.assertEqual(row["verdict"], "disqualified (critical failure)")
        self.assertTrue(any("protected_file_modified" in lbl
                             for cf in row["critical_failures"] for lbl in cf["labels"]))

    def test_no_campaign_root_means_no_protected_file_check_but_still_works(self):
        comparisons = [{"cross": {"baselines": {"amplifier-plain": {"per_task": {
            f"t{i}": {"candidate_exec_s": 8.0, "candidate_passed": True,
                      "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True}
            for i in range(10)
        }}}}, "per_harness": {"amplifier-fd": {"mean_cost_known_usd": 0.4, "unknown_cost_count": 0}}}] * 3
        anchor_comparisons = [{"per_harness": {"amplifier-plain": {"mean_cost_known_usd": 0.5,
                                                                    "unknown_cost_count": 0}}}] * 3
        row = run.build_cell_result("judge-local+effort", "plain", comparisons, anchor_comparisons,
                                     True, "holdout", 3)
        self.assertEqual(row["critical_failures"], [])
        self.assertEqual(row["verdict"], "confirmed")


class Q3ComparisonTests(unittest.TestCase):
    def test_ranks_champion_against_external_harness(self):
        champion_comparisons = [{"per_task": {
            "t1": {"amplifier-fd": {"penalized_ms": 8000.0, "outcome_passed": True}},
        }}]
        externals_comparisons = [{
            "per_harness": {"codex": {}, "amplifier-plain": {}, "amplifier-fd": {}},
            "per_task": {"t1": {"codex": {"penalized_ms": 10000.0, "outcome_passed": True}}},
        }]
        result = run.q3_comparison(champion_comparisons, externals_comparisons)
        self.assertIn("codex", result)
        self.assertAlmostEqual(result["codex"]["ratio"], 0.8)
        self.assertEqual(result["codex"]["n_paired"], 1)


class DesignRecommendationTests(unittest.TestCase):
    def _cell_row(self, cell, anchor, verdict, ratio=0.9, ci_high=0.95, p=0.01, cost_ratio=0.9,
                  gate_passed=True, vs_plain=None):
        row = {
            "cell": cell, "anchor": anchor, "reps": 3, "split": "holdout", "gate_passed": gate_passed,
            "paired_task_count": 10,
            "exec_time_ratio": {"geomean": ratio, "ci95_low": ratio - 0.1, "ci95_high": ci_high,
                                 "sign_test_p": p},
            "cost_ratio": cost_ratio, "unknown_cost_count": 0,
            "quality": {"candidate_successes": 10, "anchor_successes": 10, "non_inferior": True},
            "verdict": verdict,
        }
        if vs_plain:
            row["vs_plain"] = vs_plain
        return row

    def test_routing_beats_plain_but_not_plain_sonnet_recommends_pinning_model(self):
        results = {
            "suite": "s1", "split": "holdout", "reps": 3,
            "cells": [
                self._cell_row("judge-local+effort", "plain", "screen"),
                self._cell_row("judge-local+effort+route", "plain-sonnet", "no-effect",
                               ratio=0.99, ci_high=1.05, p=0.9,
                               vs_plain={"anchor": "plain",
                                         "exec_time_ratio": {"geomean": 0.6, "ci95_low": 0.5,
                                                              "ci95_high": 0.7, "sign_test_p": 0.001},
                                         "cost_ratio": 0.5, "quality": {}}),
            ],
            "q3": None, "evidence_limits": [],
        }
        text = run.design_recommendation_text(results, run.load_cells())
        self.assertIn("pin the cheaper model", text)
        self.assertNotIn("recommend `on`", text)

    def test_routing_beats_plain_sonnet_recommends_on(self):
        results = {
            "suite": "s1", "split": "holdout", "reps": 3,
            "cells": [
                self._cell_row("judge-local+effort", "plain", "screen"),
                self._cell_row("judge-local+effort+route", "plain-sonnet", "confirmed",
                               ratio=0.8, ci_high=0.9, p=0.01, cost_ratio=0.9),
            ],
            "q3": None, "evidence_limits": [],
        }
        text = run.design_recommendation_text(results, run.load_cells())
        self.assertIn("recommend `on`", text)

    def test_champion_confirmed_recommends_active_mode(self):
        results = {
            "suite": "s1", "split": "holdout", "reps": 3,
            "cells": [self._cell_row("judge-local+effort", "plain", "confirmed")],
            "q3": None, "evidence_limits": [],
        }
        text = run.design_recommendation_text(results, run.load_cells())
        self.assertIn("recommend `active`", text)


class RenderResultsMarkdownTests(unittest.TestCase):
    def test_contains_verdict_and_evidence_limits(self):
        results = {
            "suite": "s1", "split": "dev", "reps": 3,
            "cells": [{
                "cell": "judge-local+effort", "anchor": "plain", "reps": 3, "split": "dev",
                "gate_passed": True, "paired_task_count": 10,
                "exec_time_ratio": {"geomean": 0.85, "ci95_low": 0.7, "ci95_high": 0.95, "sign_test_p": 0.02},
                "cost_ratio": 0.9, "unknown_cost_count": 0,
                "quality": {"candidate_successes": 10, "anchor_successes": 10, "non_inferior": True},
                "verdict": "screen",
            }],
            "q3": None, "evidence_limits": ["n_tasks=10"],
        }
        md = run.render_results_markdown(results)
        self.assertIn("judge-local+effort", md)
        self.assertIn("screen", md)
        self.assertIn("n_tasks=10", md)

    def test_disqualified_verdict_and_critical_failure_notes_are_rendered(self):
        results = {
            "suite": "s1", "split": "holdout2", "reps": 3,
            "cells": [{
                "cell": "orch-haiku-shaped", "anchor": "plain", "reps": 3, "split": "holdout2",
                "gate_passed": True, "paired_task_count": 12,
                "exec_time_ratio": {"geomean": 0.7, "ci95_low": 0.6, "ci95_high": 0.8, "sign_test_p": 0.01},
                "cost_ratio": 0.8, "unknown_cost_count": 0,
                "quality": {"candidate_successes": 11, "anchor_successes": 12, "non_inferior": True},
                "critical_failures": [{"task": "repair_roman_to_int", "rep": 2,
                                       "labels": ["candidate_failed_where_anchor_passed"]}],
                "verdict": "disqualified (critical failure)",
            }],
            "q3": None, "evidence_limits": [],
        }
        md = run.render_results_markdown(results)
        self.assertIn("disqualified (critical failure)", md)
        self.assertIn("CRITICAL FAILURE", md)
        self.assertIn("repair_roman_to_int", md)
        self.assertIn("rep=2", md)


class CampaignRootTests(unittest.TestCase):
    """--campaign-root / campaign-root.txt split (STUDY-DESIGN.md section 17):
    the campaign lives outside --out by default so its long nested run-dir
    paths don't count against --out's own path budget."""

    def test_default_campaign_root_is_pure_and_never_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "myrun-out"
            root = run.default_campaign_root(out_dir)
            self.assertEqual(root, Path.home() / "dev" / "afast-ev" / "myrun-out")
            self.assertFalse(root.exists())
            self.assertFalse(out_dir.exists())

    def test_resolve_campaign_root_honors_explicit_arg_and_writes_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            campaign_dir = Path(tmp) / "elsewhere" / "campaign"
            resolved = run.resolve_campaign_root(out_dir, str(campaign_dir))
            self.assertEqual(resolved, campaign_dir.resolve())
            self.assertTrue(campaign_dir.exists())
            pointer = out_dir / "campaign-root.txt"
            self.assertEqual(pointer.read_text(encoding="utf-8").strip(), str(campaign_dir.resolve()))

    def test_resolve_campaign_root_is_idempotent_via_the_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            campaign_dir = Path(tmp) / "campaign"
            first = run.resolve_campaign_root(out_dir, str(campaign_dir))
            second = run.resolve_campaign_root(out_dir, None)  # pointer wins on the 2nd call
            self.assertEqual(first, second)

    def test_resolve_campaign_root_rejects_a_disagreeing_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            run.resolve_campaign_root(out_dir, str(Path(tmp) / "campaign-a"))
            with self.assertRaises(run.EvalsError) as ctx:
                run.resolve_campaign_root(out_dir, str(Path(tmp) / "campaign-b"))
            self.assertEqual(ctx.exception.code, 2)

    def test_resolve_campaign_root_adopts_a_pre_existing_out_nested_campaign(self):
        """A campaign directly under --out/campaign from before campaign-root.txt
        existed is adopted in place, never orphaned under a new default root."""
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "out"
            legacy_campaign = out_dir / "campaign"
            legacy_campaign.mkdir(parents=True)
            (legacy_campaign / "protocol.json").write_text("{}")
            resolved = run.resolve_campaign_root(out_dir, None)
            self.assertEqual(resolved, legacy_campaign)


class WorkspacePathLengthTests(unittest.TestCase):
    """Preflight check for the six-hour path-length outage (STUDY-DESIGN.md
    section 17): Amplifier encodes its per-project session dir 1:1 from the
    resolved workspace path, so a too-long workspace path silently kills the
    run before it launches. Reuses battery.run_dir_for rather than
    re-deriving the path, so it can never drift from what battery.py builds."""

    def test_passes_when_under_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment_dir = Path(tmp) / "exp1"
            proposal = {"frozen_run_schedule": [{"name": "short-run-a1", "task": "t1", "harness": "claude"}]}
            ok, reason, max_len = run.check_workspace_path_length(experiment_dir, proposal)
            self.assertTrue(ok)
            self.assertIsNone(reason)
            self.assertGreater(max_len, 0)

    def test_fails_loudly_with_the_longest_path_and_its_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            experiment_dir = Path(tmp) / "exp1"
            long_name = "x" * 300
            proposal = {"frozen_run_schedule": [{"name": long_name, "task": "t1", "harness": "claude"}]}
            ok, reason, max_len = run.check_workspace_path_length(experiment_dir, proposal, max_len=240)
            self.assertFalse(ok)
            self.assertIn(long_name, reason)
            self.assertIn(str(max_len), reason)
            self.assertGreater(max_len, 240)

    def test_amplifier_harness_uses_the_nested_amplifier_run_dir(self):
        """amplifier-plain/amplifier-fd runs nest one level deeper
        (runs/amplifier/<name>/workspace) -- exactly what battery.run_dir_for
        (not a re-derived path) returns for these two harnesses."""
        with tempfile.TemporaryDirectory() as tmp:
            experiment_dir = Path(tmp) / "exp1"
            proposal = {"frozen_run_schedule": [{"name": "r1", "task": "t1", "harness": "amplifier-fd"}]}
            ok, _reason, max_len = run.check_workspace_path_length(experiment_dir, proposal)
            self.assertTrue(ok)
            expected_ws = experiment_dir / "runs" / "amplifier" / "r1" / "workspace"
            expected_len = len(str(expected_ws.resolve()).replace("/", "-"))
            self.assertEqual(max_len, expected_len)

    def test_wired_into_run_verification_preflight(self):
        """run_verification records max_encoded_len at the top level of the
        preflight dict (not just nested under checks), per spec."""
        saved = {name: getattr(run, name) for name in (
            "check_permission_mode", "check_command_templates", "check_task_count",
            "check_corpus_sha", "check_frozen_candidate", "check_workspace_identity",
            "check_toolchains", "run_forge_doctor", "verify_prompts_for_experiment",
        )}
        run.check_permission_mode = lambda proposal, cell: (True, None)
        run.check_command_templates = lambda proposal, cell, defaults: (True, None)
        run.check_task_count = lambda proposal, suite, split: (True, None)
        run.check_corpus_sha = lambda proposal, suite: (True, None)
        run.check_frozen_candidate = lambda proposal, candidate_sha: (True, None)
        run.check_workspace_identity = lambda experiment_dir, proposal: (True, None, {})
        run.check_toolchains = lambda required, which=None: (True, None)
        run.run_forge_doctor = lambda host_python, forge_py, runner=None: (True, None)
        run.verify_prompts_for_experiment = lambda experiment_dir, proposal: {"ok": True, "tasks": {}}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                experiment_dir = Path(tmp) / "exp1"
                proposal = {"frozen_run_schedule": [{"name": "r1", "task": "t1", "harness": "claude"}]}
                ok, preflight, _pv = run.run_verification(
                    experiment_dir, proposal, cell={}, suite={}, split="dev", candidate_sha="x",
                    defaults={}, required_toolchains=[], host_python=sys.executable,
                    forge_py="/no/such/forge.py",
                )
        finally:
            for name, fn in saved.items():
                setattr(run, name, fn)
        self.assertTrue(ok)
        self.assertIn("workspace_path_length", preflight["checks"])
        self.assertIn("max_encoded_len", preflight)
        self.assertEqual(preflight["checks"]["workspace_path_length"]["max_encoded_len"],
                         preflight["max_encoded_len"])


class ScanIncompleteRunsTests(unittest.TestCase):
    def test_missing_result_and_no_running_json_is_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exp_dir = root / "experiments" / "EXP"
            (exp_dir / "runs").mkdir(parents=True)
            (exp_dir / "runs" / "manifest.json").write_text(json.dumps({
                "run_order": ["r1"], "runs": {"r1": {"harness": "codex"}},
            }))
            incomplete = run.scan_incomplete_runs(root, ["EXP"])
            self.assertEqual(incomplete, {"EXP": ["r1"]})

    def test_result_present_is_not_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exp_dir = root / "experiments" / "EXP"
            run_dir = exp_dir / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "result.json").write_text("{}")
            (exp_dir / "runs" / "manifest.json").write_text(json.dumps({
                "run_order": ["r1"], "runs": {"r1": {"harness": "codex"}},
            }))
            self.assertEqual(run.scan_incomplete_runs(root, ["EXP"]), {})

    def test_live_worker_is_not_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exp_dir = root / "experiments" / "EXP"
            run_dir = exp_dir / "runs" / "r1"
            run_dir.mkdir(parents=True)
            (run_dir / "running.json").write_text(json.dumps({"pid": 999999}))
            (exp_dir / "runs" / "manifest.json").write_text(json.dumps({
                "run_order": ["r1"], "runs": {"r1": {"harness": "codex"}},
            }))
            incomplete = run.scan_incomplete_runs(root, ["EXP"], pid_alive=lambda pid: True)
            self.assertEqual(incomplete, {})

    def test_missing_manifest_is_skipped_not_incomplete(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run.scan_incomplete_runs(Path(tmp), ["NOPE"]), {})


class RepsDefaultTests(unittest.TestCase):
    def test_dev_defaults_to_3_reps(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out")
            rc = run.main(["--suite", "s1", "--split", "dev", "--cells", "plain",
                           "--out", out, "--baseline-source", "/b", "--candidate-sha", "deadbeef",
                           "--dry-run"])
            self.assertEqual(rc, 0)

    def test_holdout_defaults_to_5_reps_in_dry_run_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "out"
            out_path.mkdir()
            (out_path / "PREREGISTRATION.md").write_text("x")
            out = str(out_path)
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = run.main(["--suite", "s1", "--split", "holdout", "--cells", "plain",
                               "--out", out, "--baseline-source", "/b", "--candidate-sha", "deadbeef",
                               "--dry-run"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue().strip().splitlines()[-1])
            self.assertEqual(payload["estimate"]["experiments"], 5)


class ExitCodeThreeAndSixTests(unittest.TestCase):
    def test_budget_refused_exits_3_with_partial_manifest(self):
        def fake_invoke(tool, targv):
            if tool == "campaign" and targv[0] == "init":
                root = Path(targv[targv.index("--root") + 1])
                root.mkdir(parents=True, exist_ok=True)
                (root / "protocol.json").write_text("{}")
                return {"initialized": str(root)}
            if tool == "campaign" and targv[:2] == ["budget", "status"]:
                return {"remaining": 1000.0}
            if tool == "battery" and targv[0] == "prepare":
                flags = {}
                it = iter(targv[1:])
                for a in it:
                    if a.startswith("--"):
                        flags[a] = next(it, True)
                exp_dir = battery.experiment_dir_for(Path(flags["--root"]), flags["--experiment"])
                exp_dir.mkdir(parents=True, exist_ok=True)
                (exp_dir / "proposal.json").write_text(json.dumps({
                    "tasks": ["t1"], "claude_permission_mode": "bypassPermissions",
                    "commands": {}, "task_source": None, "candidate_source_snapshot": None,
                    "frozen_run_schedule": [],
                }))
                return {"prepared": str(exp_dir)}
            if tool == "battery" and targv[0] == "run":
                raise run.EvalsError(3, "budget")
            raise AssertionError(f"unexpected invoke_tool call: {tool} {targv}")

        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = fake_invoke
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain",
                    "--reps", "1", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi", "--events-dir", "/ev",
                ])
                self.assertEqual(rc, 3)
                manifest = json.loads((Path(out) / "manifest.json").read_text())
                self.assertEqual(manifest["cells"][0]["id"], "plain")
                self.assertFalse((Path(out) / "results.json").exists())
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify

    def test_consecutive_infra_failures_pause_exits_3_with_partial_manifest(self):
        """battery.py run's circuit breaker (defect: a bursty scheduler ran 35
        experiments of garbage after Forge became unreachable) pauses with
        exit 3 and reason 'consecutive_infra_failures' -- run.py must treat
        this exactly like its existing budget/launch_cap pause: write the
        partial manifest, stop the stage, surface the reason -- never move
        on to the next cell as if nothing happened."""
        def fake_invoke(tool, targv):
            if tool == "campaign" and targv[0] == "init":
                root = Path(targv[targv.index("--root") + 1])
                root.mkdir(parents=True, exist_ok=True)
                (root / "protocol.json").write_text("{}")
                return {"initialized": str(root)}
            if tool == "campaign" and targv[:2] == ["budget", "status"]:
                return {"remaining": 1000.0}
            if tool == "battery" and targv[0] == "prepare":
                flags = {}
                it = iter(targv[1:])
                for a in it:
                    if a.startswith("--"):
                        flags[a] = next(it, True)
                exp_dir = battery.experiment_dir_for(Path(flags["--root"]), flags["--experiment"])
                exp_dir.mkdir(parents=True, exist_ok=True)
                (exp_dir / "proposal.json").write_text(json.dumps({
                    "tasks": ["t1"], "claude_permission_mode": "bypassPermissions",
                    "commands": {}, "task_source": None, "candidate_source_snapshot": None,
                    "frozen_run_schedule": [],
                }))
                return {"prepared": str(exp_dir)}
            if tool == "battery" and targv[0] == "run":
                # Mirrors battery.py's own exit-3 pause payload shape exactly
                # (see cmd_run's consecutive_infra_failures branch): reason
                # carries the diagnostic text, exit code falls back to 3.
                raise run.EvalsError(3, "consecutive_infra_failures: cannot reach http://127.0.0.1:3141/mcp")
            raise AssertionError(f"unexpected invoke_tool call: {tool} {targv}")

        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = fake_invoke
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain",
                    "--reps", "1", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi", "--events-dir", "/ev",
                ])
                self.assertEqual(rc, 3)
                manifest = json.loads((Path(out) / "manifest.json").read_text())
                self.assertEqual(manifest["cells"][0]["id"], "plain")
                self.assertTrue(manifest["cells"][0]["excluded_from_claims"])
                self.assertFalse((Path(out) / "results.json").exists())
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify

    def test_resume_with_incomplete_run_exits_6(self):
        # Build a minimal --out dir with a pre-existing manifest.json (as if a
        # prior pass completed normally) then monkeypatch scan_incomplete_runs
        # to report a gap, exercising the exit-6 wiring in main() without
        # needing a full fake battery.py pipeline.
        def fake_invoke(tool, targv):
            if tool == "campaign" and targv[0] == "init":
                return {"adopted": True}
            if tool == "campaign" and targv[:2] == ["budget", "status"]:
                return {"remaining": 1000.0}
            if tool == "battery" and targv[0] == "prepare":
                return {"prepared": True}
            if tool == "battery" and targv[0] == "run":
                return {"done": True}
            if tool == "battery" and targv[0] == "reevaluate":
                return {"changed": []}
            if tool == "battery" and targv[0] == "evaluate":
                exp = targv[targv.index("--experiment") + 1]
                mechanism = None if exp.startswith("plain-") else {
                    "mechanism_engaged": True, "mechanism_reason": None,
                    "scored_by_backend": {"ollama": 1}, "fallback_count": 0,
                    "routed_by_route": {}, "effort_routed_by_phase_effort": {"implement:high": 1},
                    "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
                }
                return {"experiment": exp, "mechanism": mechanism, "amplifier_fd_series_label": None,
                        "per_harness": {}, "cross": None, "evidence_limits": []}
            if tool == "battery_report":
                return {"report": "ok"}
            raise AssertionError(f"unexpected invoke_tool call: {tool} {targv}")

        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        orig_resume = run.resume_detection
        orig_scan = run.scan_incomplete_runs
        run.invoke_tool = fake_invoke
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        run.resume_detection = lambda *a, **k: ("reuse", None)
        run.scan_incomplete_runs = lambda campaign_root, exps: {"plain-s1-dev-r1": ["EXP-t1-a1"]}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "out"
                out.mkdir()
                (out / "campaign").mkdir()
                exp_dir = out / "campaign" / "experiments" / "plain-s1-dev-r1"
                exp_dir.mkdir(parents=True)
                (exp_dir / "proposal.json").write_text("{}")
                (exp_dir / "run-argv.json").write_text(json.dumps(
                    run.cell_to_argv("plain", _load_test_cells(), _load_test_suites(), "s1", "dev", 1,
                                      out_root=out, base_seed=20260919, baseline_source="/b",
                                      candidate_source="/c", candidate_sha="deadbeef", polyglot_root=None)
                ))
                manifest = run.build_manifest(
                    argv=["x"], suite_id="s1", split="dev", reps=1,
                    suite_doc=_load_test_suites()["suites"]["s1"],
                    candidate={"source": "/c", "requested_sha": "deadbeef", "frozen_git_sha": "deadbeef"},
                    baseline={"source": "/b"},
                    cells_report=[{"id": "plain", "experiments": ["plain-s1-dev-r1"], "seeds": [20260920],
                                   "gate": {"passed": True, "flags": []}, "excluded_from_claims": False}],
                    budget_report={}, tool_shas=run.compute_tool_shas(),
                )
                (out / "manifest.json").write_text(json.dumps(manifest))
                rc = run.main(["--resume", "--out", str(out)])
                self.assertEqual(rc, 6)
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify
            run.resume_detection = orig_resume
            run.scan_incomplete_runs = orig_scan

    def test_budget_headroom_only_failure_downgrades_to_exit_3_and_stays_resumable(self):
        """Regression for the driver stopping dead after the first experiment:
        a check_budget_headroom failure (a resource constraint, expected to
        trigger mid-matrix as reps consume the ledger, scoped to one rep) must
        take the same exit-3 partial-manifest/resumable path as battery.py
        run's own launch-cap/budget pause -- not the generic EvalsError(4)
        that aborts the entire requested cell x rep matrix with no manifest
        at all for the 12+ other cells never attempted."""
        def fake_invoke(tool, targv):
            if tool == "campaign" and targv[0] == "init":
                root = Path(targv[targv.index("--root") + 1])
                root.mkdir(parents=True, exist_ok=True)
                (root / "protocol.json").write_text("{}")
                return {"initialized": str(root)}
            if tool == "campaign" and targv[:2] == ["budget", "status"]:
                return {"remaining": 1.0}
            if tool == "battery" and targv[0] == "prepare":
                flags = {}
                it = iter(targv[1:])
                for a in it:
                    if a.startswith("--"):
                        flags[a] = next(it, True)
                exp_dir = battery.experiment_dir_for(Path(flags["--root"]), flags["--experiment"])
                exp_dir.mkdir(parents=True, exist_ok=True)
                (exp_dir / "proposal.json").write_text(json.dumps({
                    "tasks": ["t1"], "claude_permission_mode": "bypassPermissions",
                    "commands": {}, "task_source": None, "candidate_source_snapshot": None,
                    "frozen_run_schedule": [{"name": "r1", "task": "t1", "harness": "amplifier-plain"}],
                }))
                return {"prepared": str(exp_dir)}
            raise AssertionError(f"unexpected invoke_tool call past prepare: {tool} {targv}")

        def fake_verification(experiment_dir, proposal, cell, suite, split, candidate_sha,
                               defaults, required_toolchains, host_python, forge_py,
                               budget_status=None, num_runs=0, per_launch_usd=0.0,
                               which=None, doctor_runner=None):
            passed_check = {"passed": True, "reason": None}
            checks = {
                "permission_mode": passed_check, "command_templates": passed_check,
                "task_count": passed_check, "corpus_sha": passed_check,
                "frozen_candidate": passed_check, "workspace_identity": passed_check,
                "toolchains": passed_check, "forge_doctor": passed_check,
                "budget_headroom": {"passed": False,
                                     "reason": "remaining budget $1.00 < estimated $12.00 for 1 runs"},
                "prompt_identity": passed_check,
            }
            return False, {"checks": checks}, {"ok": True, "tasks": {}}

        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = fake_invoke
        run.run_verification = fake_verification
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain,judge-local+effort",
                    "--reps", "1", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi", "--events-dir", "/ev",
                ])
                self.assertEqual(rc, 3)
                manifest = json.loads((Path(out) / "manifest.json").read_text())
                cells_by_id = {c["id"]: c for c in manifest["cells"]}
                # the failing cell's own rep is recorded, excluded from claims,
                # not silently dropped
                self.assertTrue(cells_by_id["plain"]["excluded_from_claims"])
                # the second requested cell was never reached -- still gets a
                # placeholder entry so --resume knows to pick it back up later,
                # instead of vanishing with no record it was ever requested
                self.assertIn("judge-local+effort", cells_by_id)
                self.assertTrue(cells_by_id["judge-local+effort"]["excluded_from_claims"])
                self.assertFalse((Path(out) / "results.json").exists())
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify


class InvokeToolReasonExtractionTests(unittest.TestCase):
    """Regression for `battery ['prepare', '--root'] failed: None`: battery.py's
    own failure envelope varies by call site (`_fail()` prints {'error': ...},
    battery.py run's launch-cap/budget pause prints {'reason': ...}) and
    invoke_tool must surface either -- reading only 'reason' silently
    discarded the real diagnostic text whenever the subprocess used the
    {'error': ...} convention (a real, non-empty payload, so the None from a
    missing 'reason' key was never replaced by the stderr fallback either)."""

    def _run_with_fake_subprocess(self, stdout, stderr, returncode):
        proc = SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)
        with patch.object(run.subprocess, "run", return_value=proc):
            with self.assertRaises(run.EvalsError) as ctx:
                run.invoke_tool("battery", ["prepare", "--root", "/tmp/x"])
        return ctx.exception

    def test_error_key_payload_surfaces_the_real_reason(self):
        stdout = json.dumps({"error": "Experiment plain-s1-dev-r2 already exists"})
        exc = self._run_with_fake_subprocess(stdout, stderr="", returncode=4)
        self.assertIn("Experiment plain-s1-dev-r2 already exists", exc.reason)
        self.assertNotIn("None", exc.reason)
        self.assertEqual(exc.code, 4)

    def test_reason_key_payload_still_works(self):
        stdout = json.dumps({"reason": "budget", "paused": True})
        exc = self._run_with_fake_subprocess(stdout, stderr="", returncode=3)
        self.assertIn("budget", exc.reason)
        self.assertEqual(exc.code, 3)

    def test_unparseable_stdout_falls_back_to_stderr(self):
        exc = self._run_with_fake_subprocess(
            stdout="not json", stderr="Traceback: boom", returncode=1)
        self.assertIn("boom", exc.reason)

    def test_exit_key_in_payload_still_sets_the_raised_code(self):
        stdout = json.dumps({"error": "no tasks selected", "exit": 4})
        exc = self._run_with_fake_subprocess(stdout, stderr="", returncode=1)
        self.assertEqual(exc.code, 4)
        self.assertIn("no tasks selected", exc.reason)


class EndToEndResultsArtifactsTests(unittest.TestCase):
    """Drives the same two-cell sequence as EndToEndTwoCellTests, but with a
    realistic 'evaluate' fake (cross-comparison data) to exercise the new
    results.json/RESULTS.md/DESIGN-RECOMMENDATION.md generation end to end."""

    def test_results_artifacts_written(self):
        def fake_invoke(tool, argv):
            if tool == "campaign" and argv[0] == "init":
                root = Path(argv[argv.index("--root") + 1])
                root.mkdir(parents=True, exist_ok=True)
                (root / "protocol.json").write_text("{}")
                return {"initialized": str(root)}
            if tool == "campaign" and argv[:2] == ["budget", "status"]:
                return {"remaining": 1000.0}
            if tool == "battery" and argv[0] == "prepare":
                flags = {}
                it = iter(argv[1:])
                for a in it:
                    if a.startswith("--"):
                        flags[a] = next(it, True)
                exp_dir = battery.experiment_dir_for(Path(flags["--root"]), flags["--experiment"])
                exp_dir.mkdir(parents=True, exist_ok=True)
                (exp_dir / "proposal.json").write_text(json.dumps({
                    "tasks": ["t1", "t2"], "claude_permission_mode": "bypassPermissions",
                    "commands": {}, "task_source": None, "candidate_source_snapshot": None,
                    "frozen_run_schedule": [],
                }))
                return {"prepared": str(exp_dir)}
            if tool == "battery" and argv[0] == "run":
                return {"experiment": argv[argv.index("--experiment") + 1]}
            if tool == "battery" and argv[0] == "reevaluate":
                return {"experiment": argv[argv.index("--experiment") + 1], "changed": []}
            if tool == "battery" and argv[0] == "evaluate":
                exp = argv[argv.index("--experiment") + 1]
                if exp.startswith("plain-"):
                    return {"experiment": exp, "mechanism": None, "amplifier_fd_series_label": None,
                            "per_harness": {"amplifier-plain": {"mean_cost_known_usd": 1.0,
                                                                 "unknown_cost_count": 0}},
                            "per_task": {}, "cross": None, "evidence_limits": ["n_tasks=2"]}
                mechanism = {
                    "mechanism_engaged": True, "mechanism_reason": None,
                    "scored_by_backend": {"ollama": 2}, "fallback_count": 0,
                    "routed_by_route": {}, "effort_routed_by_phase_effort": {"implement:high": 2},
                    "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
                }
                cross = {"baselines": {"amplifier-plain": {"per_task": {
                    "t1": {"candidate_exec_s": 8.0, "candidate_passed": True,
                           "amplifier-plain_exec_s": 10.0, "amplifier-plain_passed": True},
                    "t2": {"candidate_exec_s": 9.0, "candidate_passed": True,
                           "amplifier-plain_exec_s": 11.0, "amplifier-plain_passed": True},
                }}}, "evidence_limits": ["cross_campaign"]}
                return {"experiment": exp, "mechanism": mechanism, "amplifier_fd_series_label": None,
                        "per_harness": {"amplifier-fd": {"mean_cost_known_usd": 0.5, "unknown_cost_count": 0}},
                        "per_task": {}, "cross": cross, "evidence_limits": ["n_tasks=2"]}
            if tool == "battery_report":
                return {"report": str(Path(argv[argv.index("--out") + 1]))}
            raise AssertionError(f"unexpected invoke_tool call: {tool} {argv}")

        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = fake_invoke
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain,judge-local+effort",
                    "--reps", "1", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi", "--events-dir", "/ev",
                ])
                self.assertEqual(rc, 0)
                results = json.loads((Path(out) / "results.json").read_text())
                row = next(r for r in results["cells"] if r["cell"] == "judge-local+effort")
                self.assertAlmostEqual(row["exec_time_ratio"]["geomean"], (8.0 / 10.0 * 9.0 / 11.0) ** 0.5)
                self.assertAlmostEqual(row["cost_ratio"], 0.5)
                self.assertTrue((Path(out) / "RESULTS.md").exists())
                self.assertTrue((Path(out) / "DESIGN-RECOMMENDATION.md").exists())
                md = (Path(out) / "RESULTS.md").read_text()
                self.assertIn("judge-local+effort", md)
                rec = (Path(out) / "DESIGN-RECOMMENDATION.md").read_text()
                self.assertIn("mode", rec)
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify



def _real_task(name, prompt):
    """A real battery_tasks.Task (frozen dataclass, `files` is a dict field --
    unhashable, which is exactly what crashed forge_workloads.task_prompt when
    it was handed the resolved object instead of the task name)."""
    return battery_tasks.Task(
        name=name, family="fam", split="dev", kind="code", prompt=prompt,
        files={"README.md": "x"}, protected=("README.md",),
        expected_answer=None, evaluate=lambda ws: {"checks": 0, "passed": 0, "failed": 0, "failure_labels": []},
    )


class VerifyPromptsForExperimentTests(unittest.TestCase):
    """Regression coverage for the get_prompt/get_task_prompt crash: passing the
    *resolved* battery_tasks.Task (unhashable, dict `files` field) into
    forge_workloads.task_prompt instead of the task name raised
    `TypeError: cannot use 'battery_tasks.Task' as a dict key`. Both the
    battery_tasks.TASKS path (S1) and the registered-extra-source / polyglot
    path (S2) go through the same forge_workloads.task_prompt(name) contract.
    """

    def test_verify_prompts_for_experiment_with_real_battery_task(self):
        task = _real_task("fake_task_1", "Do the fake thing.")
        fake_mod = ModuleType("battery_tasks")
        fake_mod.TASKS = {"fake_task_1": task}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(sys.modules, {"battery_tasks": fake_mod}):
            proposal = {"tasks": ["fake_task_1"], "frozen_run_schedule": []}
            result = run.verify_prompts_for_experiment(Path(tmp), proposal)
        self.assertTrue(result["ok"])
        self.assertTrue(result["tasks"]["fake_task_1"]["ok"])
        self.assertIn("expected_sha", result["tasks"]["fake_task_1"])

    def test_verify_prompts_for_experiment_with_registered_polyglot_task(self):
        """S2: a task resolved via forge_workloads' extra-source registry
        (the same mechanism polyglot_tasks.load populates) rather than
        battery_tasks.TASKS."""
        task = _real_task("polyglot/python/hello", "Solve the polyglot exercise.")
        with tempfile.TemporaryDirectory() as tmp, patch.dict(forge_workloads._extra_tasks, {"polyglot/python/hello": task}):
            proposal = {"tasks": ["polyglot/python/hello"], "frozen_run_schedule": []}
            result = run.verify_prompts_for_experiment(Path(tmp), proposal)
        self.assertTrue(result["ok"])
        self.assertTrue(result["tasks"]["polyglot/python/hello"]["ok"])
        self.assertIn("expected_sha", result["tasks"]["polyglot/python/hello"])


class InitOrAdoptCampaignBudgetMappingTests(unittest.TestCase):
    """cells.yaml's `budget` block must reach campaign.py's `init` as
    `budgets.<key>` -- the exact dotted keys campaign.py's `_get(proposal,
    'budgets.<key>', default)` reads (scripts/campaign.py ~lines 379-398).
    Before this fix, run.py only wrote `fast_decisions_budget`, so every
    formal run silently inherited campaign.py's tiny built-in defaults
    (estimated_total_usd=150.0, max_benchmark_worker_launches=60) instead
    of the cell's real budget.
    """

    def test_proposal_budgets_carry_cells_yaml_values(self):
        calls = []

        def fake_invoke(tool, targv):
            calls.append((tool, targv))
            return {}

        orig_invoke = run.invoke_tool
        run.invoke_tool = fake_invoke
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cells_doc = {
                    "budget": {
                        "per_launch_usd": 12.0,
                        "estimated_total_usd": 1500.0,
                        "max_benchmark_worker_launches": 2000,
                        "max_infrastructure_retries_per_run": 1,
                        "max_parallel_timed_runs": 1,
                        "wall_hours": 24,
                    }
                }
                run.init_or_adopt_campaign(
                    tmp, cells_doc, campaign_root=Path(tmp) / "campaign",
                    baseline_source="/baseline", candidate_source="/candidate",
                    installed_cache="/cache", history_index="/hist",
                    host_python=sys.executable, events_dir="/events",
                )
                proposal = json.loads((Path(tmp) / "campaign-proposal.json").read_text())
        finally:
            run.invoke_tool = orig_invoke

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "campaign")

        budgets = proposal["budgets"]
        self.assertEqual(budgets["estimated_total_usd"], 1500.0)
        self.assertEqual(budgets["max_benchmark_worker_launches"], 2000)
        self.assertEqual(budgets["per_launch_usd"], 12.0)
        self.assertEqual(budgets["max_infrastructure_retries_per_run"], 1)
        self.assertEqual(budgets["max_parallel_timed_runs"], 1)
        self.assertEqual(budgets["wall_hours"], 24)
        # provenance field retained, unchanged
        self.assertEqual(proposal["fast_decisions_budget"], cells_doc["budget"])

        # campaign.py's own `_get` -- the exact reader this fix targets --
        # must resolve the real values, not its built-in defaults.
        import campaign

        self.assertEqual(campaign._get(proposal, "budgets.estimated_total_usd", 150.0), 1500.0)
        self.assertEqual(campaign._get(proposal, "budgets.max_benchmark_worker_launches", 60), 2000)
        self.assertEqual(campaign._get(proposal, "budgets.per_launch_usd", 12.0), 12.0)

    def test_missing_budget_keys_fall_back_to_campaign_defaults(self):
        """A cell with only a partial budget block (or none) must not write
        bogus keys -- campaign.py's own defaults apply for anything absent."""
        calls = []

        def fake_invoke(tool, targv):
            calls.append((tool, targv))
            return {}

        orig_invoke = run.invoke_tool
        run.invoke_tool = fake_invoke
        try:
            with tempfile.TemporaryDirectory() as tmp:
                run.init_or_adopt_campaign(
                    tmp, {}, campaign_root=Path(tmp) / "campaign",
                    baseline_source="/baseline", candidate_source="/candidate",
                    installed_cache="/cache", history_index="/hist",
                    host_python=sys.executable, events_dir="/events",
                )
                proposal = json.loads((Path(tmp) / "campaign-proposal.json").read_text())
        finally:
            run.invoke_tool = orig_invoke

        self.assertEqual(proposal["budgets"], {})

        import campaign

        self.assertEqual(campaign._get(proposal, "budgets.estimated_total_usd", 150.0), 150.0)


class CellOrderForRepTests(unittest.TestCase):
    """Unit tests for the pure `cell_order_for_rep` helper -- section 7
    amendment (confound fix): removes the 'every batch runs cells in
    cells.yaml declared order, plain always first' confound."""

    CELL_IDS = ["plain", "plain-sonnet", "judge-local+effort"]

    def test_declared_mode_reproduces_declared_order(self):
        order = run.cell_order_for_rep(self.CELL_IDS, 1, mode="declared", base_seed=20260919)
        self.assertEqual(order, self.CELL_IDS)
        # declared mode ignores rep -- always the same
        order2 = run.cell_order_for_rep(self.CELL_IDS, 7, mode="declared", base_seed=20260919)
        self.assertEqual(order2, self.CELL_IDS)

    def test_shuffle_is_deterministic_for_a_fixed_seed_and_rep(self):
        a = run.cell_order_for_rep(self.CELL_IDS, 2, mode="shuffle", base_seed=20260919)
        b = run.cell_order_for_rep(self.CELL_IDS, 2, mode="shuffle", base_seed=20260919)
        self.assertEqual(a, b)
        self.assertEqual(sorted(a), sorted(self.CELL_IDS))

    def test_shuffle_order_is_not_always_identical_across_reps(self):
        orders = [run.cell_order_for_rep(self.CELL_IDS, rep, mode="shuffle", base_seed=20260919)
                  for rep in (1, 2, 3, 4, 5)]
        self.assertGreater(len(set(tuple(o) for o in orders)), 1,
                            "shuffled per-rep order should vary across reps for >=3 cells")
        for order in orders:
            self.assertEqual(sorted(order), sorted(self.CELL_IDS))


class RepMajorSchedulingTests(unittest.TestCase):
    """Integration tests for main()'s rep-major loop: every rep runs every
    requested cell once (in shuffled or declared order) before any cell's
    `evaluate` step for that rep, so an anchor_cell's raw run+reevaluate is
    always on disk before a dependent cell reads it as a baseline."""

    def _fake_invoke(self, reevaluated, evaluate_calls):
        def fake_invoke(tool, argv):
            if tool == "campaign" and argv[0] == "init":
                root = Path(argv[argv.index("--root") + 1])
                root.mkdir(parents=True, exist_ok=True)
                (root / "protocol.json").write_text("{}")
                return {"initialized": str(root)}
            if tool == "campaign" and argv[:2] == ["budget", "status"]:
                return {"remaining": 1000.0}
            if tool == "battery" and argv[0] == "prepare":
                flags = {}
                it = iter(argv[1:])
                for a in it:
                    if a.startswith("--"):
                        flags[a] = next(it, True)
                exp_dir = battery.experiment_dir_for(Path(flags["--root"]), flags["--experiment"])
                exp_dir.mkdir(parents=True, exist_ok=True)
                (exp_dir / "proposal.json").write_text(json.dumps({
                    "tasks": ["t1"], "claude_permission_mode": "bypassPermissions",
                    "commands": {}, "task_source": None, "candidate_source_snapshot": None,
                    "frozen_run_schedule": [],
                }))
                return {"prepared": str(exp_dir)}
            if tool == "battery" and argv[0] == "run":
                return {"experiment": argv[argv.index("--experiment") + 1]}
            if tool == "battery" and argv[0] == "reevaluate":
                exp = argv[argv.index("--experiment") + 1]
                reevaluated.add(exp)
                return {"experiment": exp, "changed": []}
            if tool == "battery" and argv[0] == "evaluate":
                exp = argv[argv.index("--experiment") + 1]
                evaluate_calls.append(exp)
                if "--baseline-experiment" in argv:
                    baseline_exp = argv[argv.index("--baseline-experiment") + 1]
                    if baseline_exp not in reevaluated:
                        raise AssertionError(
                            f"evaluate({exp}) read anchor {baseline_exp} before its run+reevaluate")
                if exp.startswith("plain-"):
                    mechanism = None
                else:
                    mechanism = {
                        "mechanism_engaged": True, "mechanism_reason": None,
                        "scored_by_backend": {"ollama": 1}, "fallback_count": 0,
                        "routed_by_route": {}, "effort_routed_by_phase_effort": {"implement:high": 1},
                        "model_routed_requested_models": {}, "model_routed_escalations_by_reason": {},
                    }
                return {"experiment": exp, "mechanism": mechanism, "amplifier_fd_series_label": None}
            if tool == "battery_report":
                return {"report": str(Path(argv[argv.index("--out") + 1]))}
            raise AssertionError(f"unexpected invoke_tool call: {tool} {argv}")
        return fake_invoke

    def test_anchor_run_precedes_candidate_evaluate_regardless_of_launch_order(self):
        reevaluated = set()
        evaluate_calls = []
        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = self._fake_invoke(reevaluated, evaluate_calls)
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain,judge-local+effort",
                    "--reps", "3", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi", "--events-dir", "/ev",
                ])
                self.assertEqual(rc, 0)
                # every rep's evaluate actually ran (no exception raised by the
                # anchor-ordering assertion embedded in the fake above)
                self.assertEqual(len(evaluate_calls), 6)  # 2 cells x 3 reps
                manifest = json.loads((Path(out) / "manifest.json").read_text())
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify

        cell_order = manifest["invocation"]["cell_order"]
        self.assertEqual(cell_order["mode"], "shuffle")
        for rep in ("1", "2", "3"):
            self.assertEqual(set(cell_order["per_rep"][rep]), {"plain", "judge-local+effort"})

        timing = manifest["timing"]
        for cid in ("plain", "judge-local+effort"):
            for rep in ("1", "2", "3"):
                entry = timing[cid][rep]
                self.assertIsNotNone(entry["started_at"])
                self.assertIsNotNone(entry["ended_at"])

    def test_declared_cell_order_flag_reproduces_old_declared_order(self):
        reevaluated = set()
        evaluate_calls = []
        orig_invoke = run.invoke_tool
        orig_verify = run.run_verification
        run.invoke_tool = self._fake_invoke(reevaluated, evaluate_calls)
        run.run_verification = lambda *a, **k: (True, {"checks": {}}, {"ok": True, "tasks": {}})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = str(Path(tmp) / "out")
                rc = run.main([
                    "--suite", "s1", "--split", "dev", "--cells", "plain,judge-local+effort",
                    "--reps", "2", "--out", out, "--baseline-source", "/b",
                    "--candidate-source", "/c", "--candidate-sha", "deadbeef",
                    "--installed-cache", "/ic", "--history-index", "/hi", "--events-dir", "/ev",
                    "--cell-order", "declared",
                ])
                self.assertEqual(rc, 0)
                manifest = json.loads((Path(out) / "manifest.json").read_text())
        finally:
            run.invoke_tool = orig_invoke
            run.run_verification = orig_verify

        cell_order = manifest["invocation"]["cell_order"]
        self.assertEqual(cell_order["mode"], "declared")
        declared = run.resolve_cell_ids("plain,judge-local+effort", _load_test_cells())
        for rep in ("1", "2"):
            self.assertEqual(cell_order["per_rep"][rep], declared)


if __name__ == "__main__":
    unittest.main()
