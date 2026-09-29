"""Offline parity: the shipped v3 delegation policy vs the queue the paid run used.

Replays the RECORDED judge answers (`classify_v2.min.jsonl`, `depth.min.jsonl`)
through `DelegationPolicy` and compares the lever it picks with the lever
recorded in `v3_queue.min.jsonl` -- the queue the paid A/B evaluation actually
ran (docs/DELEGATION-ROUTING.md, "Evidence"). No network, no judge call, no LLM.

The fixtures are the minimum needed to re-derive a decision: the tier
probabilities, the depth answer, the anchor, and the recorded target. They carry
no task text (see tests/fixtures/delegation/README.md).

One deliberate difference is expected and asserted BY NAME (KNOWN_DIVERGENCES).
Any other mismatch fails.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from amplifier_fast_decisions.delegation import JudgeAnswers
from amplifier_fast_decisions.delegation_policy import DelegationPolicy

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "delegation"
CLASSIFY = FIXTURES / "classify_v2.min.jsonl"
DEPTH = FIXTURES / "depth.min.jsonl"
QUEUE = FIXTURES / "v3_queue.min.jsonl"

#: sample_id -> why the shipped policy deliberately decides differently from the
#: queue. This came from the evaluation's OWN result, which landed after the
#: queue had already been built.
KNOWN_DIVERGENCES = {
    "s134": (
        "guard never_lower_effort_on_tiers: the anchor is haiku (small tier). The "
        "queue lowered effort here and it FAILED (arm A acceptable, arm B not) -- "
        "that result is exactly why the guard exists, so the policy now abstains."
    ),
}


def _load(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _answers(classify_row: dict, depth: str) -> JudgeAnswers:
    probabilities = classify_row["min_tier_probabilities"]
    top = max(probabilities, key=probabilities.get)
    return JudgeAnswers(
        min_tier=top,
        min_tier_p=probabilities[top],
        answer_depth=depth,
        # The recorded run never asked these; unknown requirements block nothing.
        needs_computer_use=None,
        needs_vision=None,
        probabilities={"min_tier": dict(probabilities)},
    )


def _anchor(row: dict) -> list[dict]:
    provider, _, model = row["anchor_candidate"].partition("/")
    return [{"provider": provider, "model": model, "config": {}}]


def _expected(row: dict) -> tuple[str, str | None]:
    """(lever, model) the queue recorded; A_MODEL means "the anchor's own model"."""
    target = row["b_target"]
    model = target["model"]
    if model == "A_MODEL":
        model = row["anchor_candidate"].partition("/")[2]
    return target["lever"], model


@unittest.skipUnless(
    CLASSIFY.exists() and DEPTH.exists() and QUEUE.exists(),
    "recorded replay fixtures are absent",
)
class DelegationPolicyParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = DelegationPolicy.load("v3")
        cls.classify = {r["sample_id"]: r for r in _load(CLASSIFY)}
        cls.depth = {r["sample_id"]: r["answer_depth"] for r in _load(DEPTH)}
        cls.queue = _load(QUEUE)

    def test_anchor_tier_matches_the_harness(self):
        """Tier derivation must agree with the harness, or nothing else can."""
        for row in self.queue:
            model = row["anchor_candidate"].partition("/")[2]
            self.assertEqual(self.policy.tier_of(model), row["anchor_tier"], row["sample_id"])

    def test_est_input_tokens_matches_the_harness(self):
        for row in self.queue:
            self.assertEqual(
                self.policy.est_input_tokens(row["instr_len"]), row["est_input_tokens"]
            )

    def test_v3_policy_reproduces_the_recorded_queue(self):
        self.assertTrue(self.queue, "empty queue fixture")
        matched: list[str] = []
        diverged: list[str] = []
        mismatches: list[str] = []

        for row in self.queue:
            sample = row["sample_id"]
            self.assertIn(sample, self.classify, f"{sample}: missing recorded answers")
            self.assertIn(sample, self.depth, f"{sample}: missing recorded answers")
            choice = self.policy.choose(
                _anchor(row),
                _answers(self.classify[sample], self.depth[sample]),
                row["instr_len"],
            )
            want_lever, want_model = _expected(row)

            if sample in KNOWN_DIVERGENCES:
                # A documented divergence must actually diverge -- and by
                # abstaining, never by silently picking some other target.
                self.assertIsNone(
                    choice.final,
                    f"{sample}: expected the documented divergence "
                    f"({KNOWN_DIVERGENCES[sample]}), but the policy chose {choice.final}",
                )
                diverged.append(sample)
                continue

            if choice.final is None:
                mismatches.append(f"{sample}: abstained ({choice.reason}); queue={want_lever}")
            elif (choice.lever, choice.final["model"]) != (want_lever, want_model):
                mismatches.append(
                    f"{sample}: policy={choice.lever}/{choice.final['model']} "
                    f"queue={want_lever}/{want_model}"
                )
            else:
                matched.append(sample)

        self.assertEqual(mismatches, [], "unexplained mismatches")
        # The headline numbers quoted in docs/DELEGATION-ROUTING.md.
        self.assertEqual(len(matched), 20)
        self.assertEqual(diverged, ["s134"])
        self.assertEqual(len(matched) + len(diverged), len(self.queue))

    def test_small_tier_guard_abstains_on_the_sample_that_failed(self):
        """s134's guard, stated directly rather than only via the queue replay."""
        anchor = [{"provider": "anthropic", "model": "claude-haiku-4-5", "config": {}}]
        answers = JudgeAnswers(
            min_tier="small", min_tier_p=0.95, answer_depth="standard",
        )
        choice = self.policy.choose(anchor, answers, 1000)
        self.assertIsNone(choice.final)
        self.assertEqual(choice.guard, "never_lower_effort_on_tiers")

    def test_effort_lever_never_changes_provider_or_model(self):
        for row in self.queue:
            sample = row["sample_id"]
            anchor = _anchor(row)
            choice = self.policy.choose(
                anchor, _answers(self.classify[sample], self.depth[sample]), row["instr_len"]
            )
            if choice.lever != "effort":
                continue
            self.assertEqual(choice.final["provider"], anchor[0]["provider"])
            self.assertEqual(choice.final["model"], anchor[0]["model"])
            self.assertEqual(choice.final["config"]["reasoning_effort"], "low")


class CapabilityTableTests(unittest.TestCase):
    """Verified facts block a candidate; UNKNOWN never does."""

    @classmethod
    def setUpClass(cls):
        cls.policy = DelegationPolicy.load("v3")

    def test_a_verified_false_capability_blocks_the_candidate(self):
        conflict = self.policy._capability_conflict(
            "claude-sonnet-5-5", {"needs_computer_use": True}
        )
        self.assertEqual(conflict["capability"], "computer_use")

    def test_an_unrecorded_model_is_unknown_and_therefore_allowed(self):
        self.assertIsNone(
            self.policy._capability_conflict(
                "claude-haiku-4-5-20251001", {"needs_computer_use": True}
            )
        )

    def test_an_unrecorded_capability_is_unknown_and_therefore_allowed(self):
        self.assertIsNone(
            self.policy._capability_conflict("claude-sonnet-5-5", {"needs_vision": True})
        )

    def test_a_requirement_that_is_not_needed_blocks_nothing(self):
        self.assertIsNone(
            self.policy._capability_conflict(
                "claude-sonnet-5-5", {"needs_computer_use": False}
            )
        )


if __name__ == "__main__":
    unittest.main()
