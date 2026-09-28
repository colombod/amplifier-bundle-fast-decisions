"""Restricted-softmax label scoring prototype (score_backend). No model, no network."""
from __future__ import annotations

import asyncio
import math
import unittest

from amplifier_fast_decisions.backends import BackendUnavailable
from amplifier_fast_decisions.contracts import DecisionRequest, Question
from amplifier_fast_decisions.score_backend import (
    OTHER, LabelTokenError, ScoreBackend, close_open_think, combine_orders, cyclic_orders,
    restricted_softmax, score_question, verify_label_ids,
)

CHOICE = Question(name="difficulty", type="choice", instructions="Simple or complex?",
                  criteria={"simple": "small fix", "complex": "large investigation"})
NOUL = Question(name="urgent", type="noul", instructions="Is it urgent?")


class CharTokenizer:
    """One token per character, except a few merged pieces -- enough to
    exercise position-dependent single-token checks."""

    MERGES = {" A": 1000, " B": 1001, " C": 1002, "AB": 1003}

    def encode(self, text):
        out, i = [], 0
        while i < len(text):
            pair = text[i:i + 2]
            if pair in self.MERGES:
                out.append(self.MERGES[pair])
                i += 2
            else:
                out.append(ord(text[i]))
                i += 1
        return out


class FakeScorer:
    """Label logits chosen per prompt: prefers the option shown first by
    ``position_bias`` nats, and 'complex' by ``pref`` nats."""

    model = "fake"

    def __init__(self, pref=1.0, position_bias=0.0, off_label_logit=None):
        self.tok = CharTokenizer()
        self.pref, self.position_bias, self.off = pref, position_bias, off_label_logit
        self.calls = 0

    def render(self, system, user):
        return f"<s>{system}\n{user}\n<assistant>"

    def encode(self, text):
        return self.tok.encode(text)

    def label_logprobs(self, tokens, ids):
        self.calls += 1
        pieces = {v: k for k, v in CharTokenizer.MERGES.items()}
        text = "".join(pieces.get(t) or chr(t) for t in tokens)
        shown = {line[0]: line[3:] for line in text.splitlines() if len(line) > 3 and line[1:3] == ". "}
        logits = []
        for i in ids:
            letter = (pieces.get(i) or chr(i)).strip()
            shows_complex = shown.get(letter, "").startswith("large")
            logits.append((self.pref if shows_complex else 0.0) + (self.position_bias if letter == "A" else 0.0))
        # full-vocab normaliser: labels plus an off-label token
        others = [self.off] if self.off is not None else []
        z = math.log(sum(math.exp(v) for v in logits + others))
        lp = [v - z for v in logits]
        return lp, sum(math.exp(v) for v in lp)


class PureHelpers(unittest.TestCase):
    def test_restricted_softmax_ignores_shared_normaliser(self):
        a = restricted_softmax({"x": -1.0, "y": -2.0})
        b = restricted_softmax({"x": -11.0, "y": -12.0})
        for k in a:
            self.assertAlmostEqual(a[k], b[k])
        self.assertAlmostEqual(sum(a.values()), 1.0)

    def test_verify_label_ids_is_position_dependent(self):
        tok = CharTokenizer()
        # After ":" both the bare letter and " A" are single appended tokens.
        ids = verify_label_ids(tok.encode, "Answer:", ["A", "B"])
        self.assertEqual(ids["A"], [ord("A"), 1000])
        # After a prefix ending in "A", appending "B" merges into "AB": not one appended token for B.
        with self.assertRaises(LabelTokenError):
            verify_label_ids(tok.encode, "xA", ["B"], variants=("",))

    def test_verify_label_ids_rejects_multi_token_labels(self):
        tok = CharTokenizer()
        with self.assertRaises(LabelTokenError):
            verify_label_ids(tok.encode, "p", ["Yes"])

    def test_close_open_think(self):
        self.assertTrue(close_open_think("<|im_start|>assistant\n<think>\n").endswith("</think>\n\n"))
        closed = "<|im_start|>assistant\n<think>\n\n</think>\n\n"
        self.assertEqual(close_open_think(closed), closed)

    def test_geometric_combine_removes_additive_position_bias(self):
        # Same preference, position bias b on whichever option is shown first.
        b, pref = 2.0, 0.5
        fwd = restricted_softmax({"simple": b, "complex": pref})
        rev = restricted_softmax({"complex": pref + b, "simple": 0.0})
        geo = combine_orders([fwd, rev], "geo")
        self.assertAlmostEqual(geo["complex"], 1 / (1 + math.exp(-pref)))
        mean = combine_orders([fwd, rev], "mean")
        self.assertNotAlmostEqual(mean["complex"], geo["complex"], places=3)

    def test_cyclic_orders_cover_every_position(self):
        orders = cyclic_orders(3)
        for pos in range(3):
            self.assertEqual({o[pos] for o in orders}, {0, 1, 2})
        self.assertEqual(cyclic_orders(2), [[0, 1], [1, 0]])


class ScoreQuestionTests(unittest.TestCase):
    def test_debiased_answer_matches_true_preference(self):
        s = FakeScorer(pref=1.0, position_bias=3.0)
        scored = score_question(s, {"task": "x"}, CHOICE)
        self.assertAlmostEqual(scored.answer.probabilities["complex"], 1 / (1 + math.exp(-1.0)), places=6)
        self.assertEqual(s.calls, 2)
        self.assertEqual(len(scored.per_order), 2)

    def test_label_mass_reported_and_floor_abstains(self):
        s = FakeScorer(pref=1.0, off_label_logit=10.0)  # the model would rather emit prose
        scored = score_question(s, {"task": "x"}, CHOICE)
        self.assertLess(max(scored.label_mass_full_vocab), 0.01)
        self.assertIsNotNone(scored.answer)  # restricted softmax still defined...
        scored = score_question(s, {"task": "x"}, CHOICE, min_label_mass=0.05)
        self.assertIsNone(scored.answer)  # ...but the floor turns it into an abstention

    def test_other_option_mass_is_reported_not_renormalised_silently(self):
        s = FakeScorer(pref=0.0)
        scored = score_question(s, {"task": "x"}, CHOICE, other=True, other_abstain=0.99)
        self.assertIsNotNone(scored.p_other)
        self.assertEqual(set(scored.answer.probabilities), {"simple", "complex"})
        self.assertEqual(s.calls, 3)  # three cyclic shifts for three options
        abstained = score_question(s, {"task": "x"}, CHOICE, other=True, other_abstain=0.0)
        self.assertIsNone(abstained.answer)

    def test_noul(self):
        scored = score_question(FakeScorer(), {"task": "x"}, NOUL)
        self.assertIsNotNone(scored.answer.noul)


class BackendAdapterTests(unittest.TestCase):
    def test_ask_questions_only(self):
        backend = ScoreBackend(FakeScorer(pref=2.0))
        result = asyncio.run(backend.ask(DecisionRequest(state={"task": "x"}, candidates=(), questions=(CHOICE,))))
        self.assertGreater(result.answers["difficulty"].probabilities["complex"], 0.8)
        self.assertEqual(result.output_tokens, 0)
        self.assertFalse(backend.external)

    def test_abstention_raises_backend_unavailable(self):
        backend = ScoreBackend(FakeScorer(pref=0.0), other=True)
        backend_other = ScoreBackend(FakeScorer(pref=0.0, off_label_logit=10.0), min_label_mass=0.5)
        req = DecisionRequest(state={"task": "x"}, candidates=(), questions=(CHOICE,))
        asyncio.run(backend.ask(req))  # OTHER mass 1/3 < 0.5: answers
        with self.assertRaises(BackendUnavailable):
            asyncio.run(backend_other.ask(req))
        self.assertEqual(backend.last["difficulty"].per_order[0].keys(), {"simple", "complex", OTHER})


if __name__ == "__main__":
    unittest.main()
