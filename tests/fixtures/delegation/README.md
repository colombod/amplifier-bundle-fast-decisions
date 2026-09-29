# Delegation routing replay fixtures

The minimum needed to re-derive one delegation decision offline, recorded from
the paid A/B evaluation described in `docs/DELEGATION-ROUTING.md`:

| File | Rows | Fields |
|---|---|---|
| `classify_v2.min.jsonl` | 200 | `sample_id`, `anchor_role`, `instr_len`, `min_tier_probabilities` |
| `depth.min.jsonl` | 200 | `sample_id`, `answer_depth` |
| `v3_queue.min.jsonl` | 21 | `sample_id`, `anchor_candidate`, `anchor_tier`, `instr_len`, `est_input_tokens`, `depth`, `decision`, `queue_pos`, `b_target` |

**No task text.** Every field above is a label, a count or a probability; the
delegated instructions these answers were derived from are not here, only their
lengths. `tests/test_delegation_parity_v3.py` replays them through the shipped
policy and asserts 20/21 agreement with the queue that actually ran, plus one
divergence (`s134`) documented by name.
