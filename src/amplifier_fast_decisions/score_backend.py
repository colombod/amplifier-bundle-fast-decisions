"""Research prototype: Jev-style fixed-choice scoring from FULL next-token logits.

Unlike ``local_backend`` (which reads the first *generated* token's top-20
``top_logprobs`` from Ollama / mlx-lm and folds whitespace variants), this
module follows the reference recipe exactly:

1. Render the prompt through the model's own chat template with thinking
   disabled and the generation prompt appended, so the next position is the
   answer position. Thinking-only templates that leave ``<think>`` open are
   closed explicitly (``</think>``) instead of prefilling off-template text.
2. Verify every label is exactly ONE token *at that position* (tokenize
   ``prefix + label`` and require ``prefix_ids + [id]``), for the bare and
   the leading-space variant; refuse otherwise.
3. Run one forward pass (no generation, no sampler) and read the logits of
   exactly those label ids; softmax over them only (restricted softmax).
   The full-vocabulary mass on the labels is reported as a diagnostic, never
   used to renormalise silently.
4. Debias position with cyclic shifts of the option list (forward/reversed
   for two options), combined in log space (geometric mean) by default.
5. Optional OTHER option: its mass is reported and, above a threshold, the
   question abstains (the caller falls back to rules) instead of being
   renormalised into the real options.

Two in-process scorers are provided (heavy deps imported lazily, loopback
only by construction since nothing leaves the process): ``LlamaCppScorer``
(llama-cpp-python over a GGUF file -- e.g. the exact blob Ollama serves) and
``MlxScorer`` (mlx-lm, Apple silicon). ``ScoreBackend`` adapts either to the
``DecisionBackend.ask`` contract for questions-only requests.

Probabilities are uncalibrated model scores; measure them on labeled data.
"""
from __future__ import annotations

import asyncio
import math
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

from .backends import BackendUnavailable
from .contracts import SLOW, Answer, DecisionRequest, DecisionResult, Question
from .local_backend import QUESTION_SYSTEM, _placeholder_action, _question_options, build_question_prompt

OTHER = "__other__"
OTHER_TEXT = "None of the options above fits, or the state does not say enough to choose."
PROBABILITY_KIND = "restricted_softmax_label_logits"


class LabelTokenError(BackendUnavailable):
    """A label is not exactly one token at the answer position."""


# --------------------------------------------------------------------------- pure helpers


def logsumexp(values: Sequence[float]) -> float:
    m = max(values)
    if m == -math.inf:
        return -math.inf
    return m + math.log(sum(math.exp(v - m) for v in values))


def restricted_softmax(label_logits: dict[str, float]) -> dict[str, float]:
    """Softmax over exactly the given labels (logits or full-vocab logprobs --
    the result is identical because the shared normaliser cancels)."""
    if not label_logits:
        raise ValueError("no labels")
    z = logsumexp(list(label_logits.values()))
    return {k: math.exp(v - z) for k, v in label_logits.items()}


def verify_label_ids(encode: Callable[[str], list[int]], prefix: str, labels: Sequence[str],
                     *, variants: Sequence[str] = ("", " ")) -> dict[str, list[int]]:
    """``{label: [token ids]}`` -- every variant (bare, leading space, ...)
    that tokenizes to exactly ``encode(prefix) + [id]``. A label with no
    single-token variant at this position, or two labels sharing an id,
    raises ``LabelTokenError`` (never read the wrong logit)."""
    base = encode(prefix)
    out: dict[str, list[int]] = {}
    for label in labels:
        ids = []
        for v in variants:
            full = encode(prefix + v + label)
            if len(full) == len(base) + 1 and full[:len(base)] == base and full[-1] not in ids:
                ids.append(full[-1])
        if not ids:
            raise LabelTokenError(f"label {label!r} is not a single token at the answer position")
        out[label] = ids
    seen: dict[int, str] = {}
    for label, ids in out.items():
        for i in ids:
            if i in seen:
                raise LabelTokenError(f"labels {seen[i]!r} and {label!r} share token id {i}")
            seen[i] = label
    return out


def close_open_think(rendered: str) -> str:
    """Thinking-only chat templates (e.g. Qwen3-*-Thinking-2507) end the
    generation prompt with an open ``<think>``; close it so the next token is
    the answer, matching the hybrid templates' ``enable_thinking=False`` form."""
    stripped = rendered.rstrip()
    if stripped.endswith("<think>"):
        return stripped + "\n\n</think>\n\n"
    return rendered


def combine_orders(dists: Sequence[dict[str, float]], mode: str = "geo") -> dict[str, float]:
    """Combine per-order distributions (keyed by option name). ``geo``:
    geometric mean then renormalise -- removes an additive logit position
    bias exactly (AnyJev L0 default). ``mean``: arithmetic mean (our current
    ``average_answers``)."""
    keys = list(dists[0])
    if mode == "mean":
        return {k: sum(d[k] for d in dists) / len(dists) for k in keys}
    if mode != "geo":
        raise ValueError("mode must be 'geo' or 'mean'")
    logs = {k: sum(math.log(max(d[k], 1e-300)) for d in dists) / len(dists) for k in keys}
    return restricted_softmax(logs)


def cyclic_orders(n: int) -> list[list[int]]:
    """Every option at every position once (for n == 2: forward + reversed)."""
    return [[(s + i) % n for i in range(n)] for s in range(n)]


# --------------------------------------------------------------------------- scorers


class LabelScorer(Protocol):
    model: str

    def render(self, system: str, user: str) -> str: ...
    def encode(self, text: str) -> list[int]: ...
    def label_logprobs(self, tokens: list[int], ids: Sequence[int]) -> tuple[list[float], float]: ...


def _lcp(a: Sequence[int], b: Sequence[int]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


class LlamaCppScorer:
    """Full next-token logits from llama.cpp (llama-cpp-python, Metal). Point
    it at the GGUF blob Ollama serves to score the identical weights."""

    def __init__(self, model_path: str, *, model: str | None = None, n_ctx: int = 4096):
        try:
            from llama_cpp import Llama
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendUnavailable("Install llama-cpp-python for LlamaCppScorer") from exc
        self.model = model or os.path.basename(model_path)
        # One ubatch for the whole bounded prompt (<= n_ctx): prefill in a single Metal pass.
        self._llm = Llama(model_path=model_path, n_ctx=n_ctx, n_batch=n_ctx, n_ubatch=n_ctx, n_gpu_layers=-1,
                          flash_attn=True, logits_all=False, verbose=False)
        self._template = self._llm.metadata.get("tokenizer.chat_template")
        if not self._template:
            raise BackendUnavailable("GGUF has no embedded chat template")
        self._tokens: list[int] = []

    def render(self, system: str, user: str) -> str:
        from jinja2.sandbox import ImmutableSandboxedEnvironment

        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        env.globals["raise_exception"] = lambda msg: (_ for _ in ()).throw(ValueError(msg))
        env.globals["strftime_now"] = lambda fmt: __import__("time").strftime(fmt)
        vocab = self._llm
        text = env.from_string(self._template).render(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            add_generation_prompt=True, enable_thinking=False,
            bos_token=vocab.detokenize([vocab.token_bos()]).decode(errors="ignore") if vocab.token_bos() >= 0 else "",
            eos_token=vocab.detokenize([vocab.token_eos()]).decode(errors="ignore"))
        return close_open_think(text)

    def encode(self, text: str) -> list[int]:
        # special=True: template control tokens (<|im_start|>...) map to their ids.
        return self._llm.tokenize(text.encode("utf-8"), add_bos=False, special=True)

    def label_logprobs(self, tokens: list[int], ids: Sequence[int]) -> tuple[list[float], float]:
        import numpy as np

        llm = self._llm
        keep = min(_lcp(self._tokens, tokens), len(tokens) - 1)  # prefix-cache reuse, >=1 new token
        llm.n_tokens = keep
        llm.eval(tokens[keep:])
        self._tokens = list(tokens)
        logits = np.ctypeslib.as_array(llm._ctx.get_logits_ith(-1), shape=(llm.n_vocab(),)).astype(np.float64)
        m = logits.max()
        lse = m + math.log(float(np.exp(logits - m).sum()))
        lp = [float(logits[i] - lse) for i in ids]
        return lp, float(np.exp(np.array(lp)).sum())


class MlxScorer:
    """Full next-token logits from mlx-lm (Apple silicon), in process."""

    def __init__(self, path: str, *, model: str | None = None):
        try:
            from mlx_lm import load
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BackendUnavailable("Install mlx-lm for MlxScorer") from exc
        self.model = model or path
        self._model, self._tok = load(path)
        self._snap = None
        self._snap_tokens: list[int] = []

    def render(self, system: str, user: str) -> str:
        text = self._tok.apply_chat_template(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            add_generation_prompt=True, tokenize=False, enable_thinking=False)
        return close_open_think(text)

    def encode(self, text: str) -> list[int]:
        return list(self._tok.encode(text, add_special_tokens=False))

    def _advance(self, cache, tokens: list[int]) -> None:
        """Feed tokens into the cache without materialising their logits
        (lazy graph: the lm_head output is never evaluated)."""
        import mlx.core as mx

        if tokens:
            self._model(mx.array(tokens)[None], cache=cache)
            mx.eval([c.state for c in cache])

    def prime(self, prefix_tokens: list[int]) -> None:
        """Snapshot the KV/recurrent state after a prefix shared by every
        option order (state + question), so each order only pays its suffix.
        Works for hybrid linear-attention caches that cannot be trimmed."""
        from mlx_lm.models.cache import make_prompt_cache

        if len(prefix_tokens) < 2 or self._snap_tokens == prefix_tokens:
            return
        cache = make_prompt_cache(self._model)
        self._advance(cache, prefix_tokens)
        self._snap = [(c.state, c.meta_state) for c in cache]
        self._snap_tokens = list(prefix_tokens)

    def _restore(self):
        from mlx_lm.models.cache import make_prompt_cache

        cache = make_prompt_cache(self._model)
        for c, (state, meta) in zip(cache, self._snap):
            c.state = list(state) if isinstance(state, list) else state  # never alias the snapshot's list
            c.meta_state = meta
        return cache

    def label_logprobs(self, tokens: list[int], ids: Sequence[int]) -> tuple[list[float], float]:
        import mlx.core as mx

        n = len(self._snap_tokens)
        if self._snap is not None and n < len(tokens) and tokens[:n] == self._snap_tokens:
            cache, keep = self._restore(), n
        else:
            from mlx_lm.models.cache import make_prompt_cache
            cache, keep = make_prompt_cache(self._model), 0
        self._advance(cache, tokens[keep:-1])
        logits = self._model(mx.array(tokens[-1:])[None], cache=cache)[0, -1].astype(mx.float32)
        lp_all = logits - mx.logsumexp(logits)
        lp = lp_all[mx.array(list(ids))].tolist()
        return lp, float(sum(math.exp(v) for v in lp))


# --------------------------------------------------------------------------- question scoring


@dataclass
class ScoredQuestion:
    answer: Answer | None
    p_other: float | None = None
    label_mass_full_vocab: list[float] = field(default_factory=list)
    per_order: list[dict[str, float]] = field(default_factory=list)
    label_ids: dict[str, list[int]] = field(default_factory=dict)


def _permuted_question(question: Question, order: list[int], options: list[tuple[str, str]]) -> Question:
    crit = {options[i][0]: options[i][1] for i in order}
    return Question(name=question.name, type="choice", instructions=question.instructions, criteria=crit)


_VERIFIED: dict[tuple, dict[str, list[int]]] = {}


def _verified_ids(scorer: LabelScorer, prefix: str, labels: list[str]) -> dict[str, list[int]]:
    # Verified on the full rendered prefix once per (scorer, labels, template tail);
    # the answer position's context is the template tail, identical across states.
    key = (id(scorer), tuple(labels), prefix[-200:])
    if key not in _VERIFIED:
        _VERIFIED[key] = verify_label_ids(scorer.encode, prefix, labels)
    return _VERIFIED[key]


def score_question(scorer: LabelScorer, state: Any, question: Question, *, combine: str = "geo",
                   other: bool = False, other_abstain: float = 0.5, system: str = QUESTION_SYSTEM,
                   answer_prefix: str = "", min_label_mass: float = 0.0) -> ScoredQuestion:
    """Restricted-softmax answer for one typed question, debiased over cyclic
    shifts of the option list. Same prompt text as ``build_question_prompt``
    (so results are comparable with the Ollama path).

    ``answer_prefix`` (e.g. ``"Answer:"``) is appended AFTER the rendered
    generation prompt (after the closed think block), for models that still
    prefer prose at the bare answer position. ``min_label_mass``: abstain when
    the full-vocabulary mass on the labels is below it in any order (the
    restricted softmax of labels the model does not want to emit is noise)."""
    options = _question_options(question)
    if other:
        options = options + [(OTHER, OTHER_TEXT)]
    per_order, masses, ids_used = [], [], {}
    rendered = []
    for order in cyclic_orders(len(options)):
        user, letter_to_name = build_question_prompt(state, _permuted_question(question, order, options))
        prefix = scorer.render(system, user) + answer_prefix
        rendered.append((prefix, scorer.encode(prefix), letter_to_name))
    prime = getattr(scorer, "prime", None)
    if prime is not None:
        common = rendered[0][1]
        for _, toks, _ in rendered[1:]:
            common = common[:_lcp(common, toks)]
        prime(common)
    for prefix, tokens, letter_to_name in rendered:
        label_ids = _verified_ids(scorer, prefix, list(letter_to_name))
        flat = [i for ids in label_ids.values() for i in ids]
        lp, mass = scorer.label_logprobs(tokens, flat)
        by_id = dict(zip(flat, lp))
        # A label's score sums its single-token variants (bare + leading space).
        label_lp = {letter_to_name[lab]: logsumexp([by_id[i] for i in ids]) for lab, ids in label_ids.items()}
        per_order.append(restricted_softmax(label_lp))
        masses.append(mass)
        ids_used = label_ids
    if min(masses) < min_label_mass:
        return ScoredQuestion(None, None, masses, per_order, ids_used)
    dist = combine_orders(per_order, combine)
    p_other = dist.pop(OTHER, None) if other else None
    if p_other is not None and p_other >= other_abstain:
        return ScoredQuestion(None, p_other, masses, per_order, ids_used)
    total = sum(dist.values())
    dist = {k: v / total for k, v in dist.items()}
    if question.type == "noul":
        answer = Answer(noul=dist["true"])
    else:
        answer = Answer(probabilities=dist, confidence=max(dist.values()))
    return ScoredQuestion(answer, p_other, masses, per_order, ids_used)


class ScoreBackend:
    """``DecisionBackend`` adapter (questions only) over an in-process scorer."""

    name = "local-score"
    external = False

    def __init__(self, scorer: LabelScorer, *, combine: str = "geo", other: bool = False,
                 answer_prefix: str = "", min_label_mass: float = 0.0):
        self.scorer = scorer
        self.answer_prefix = answer_prefix
        self.min_label_mass = min_label_mass
        self.model = scorer.model
        self.combine = combine
        self.other = other
        self._lock = asyncio.Lock()
        self.last: dict[str, ScoredQuestion] = {}

    async def answer_question(self, state: Any, question: Question) -> Answer:
        async with self._lock:
            scored = await asyncio.to_thread(score_question, self.scorer, state, question,
                                             combine=self.combine, other=self.other,
                                             answer_prefix=self.answer_prefix, min_label_mass=self.min_label_mass)
        self.last[question.name] = scored
        if scored.answer is None:
            raise BackendUnavailable("Local scorer abstained (OTHER or low label mass)")
        return scored.answer

    async def ask(self, request: DecisionRequest) -> DecisionResult:
        if request.candidates:
            raise BackendUnavailable("ScoreBackend prototype scores questions only")
        answers = {q.name: await self.answer_question(request.state, q) for q in request.questions}
        return DecisionResult(action=_placeholder_action(self.model), answers=answers, model=self.model,
                              output_tokens=0)

    async def close(self) -> None:
        return None


__all__ = ["OTHER", "SLOW", "LabelTokenError", "LlamaCppScorer", "MlxScorer", "ScoreBackend", "ScoredQuestion",
           "close_open_think", "combine_orders", "cyclic_orders", "restricted_softmax", "score_question",
           "verify_label_ids"]
