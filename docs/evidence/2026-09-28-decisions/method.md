# Experiment design and accounting

- Decision scorer smoke: 12 hand-labelled public operation-selection cases,
  2 candidate orders, 2 repetitions, Jev and local upstream Laya; 48 calls per
  backend. Includes negation/distractors. No fitting or threshold tuning.
- Host comparison: two existing public tasks (code comprehension and duration
  parser repair), ordinary Amplifier vs Jev prepared reads vs Laya prepared reads,
  2 repetitions in reversed order, same Sonnet model and no extended thinking.
  Neither fast arm changes the generative model. Threshold 0.90, margin 0.20.
- Retrieval comparison: one public code-location task, ordinary tools vs added
  jevgrep, 2 repetitions with order reversed. Same prompt/model/starting files.
- Task checks run outside agent workspaces; protected files are checked.
  All completed matched runs are included, including failures and abstentions.
- Wall time includes CLI startup. Local Laya is already resident; model download,
  load and startup are excluded from the warm comparison. Hardware and provider
  latency can vary; this is a small illustrative experiment, not a general claim.
- Provider costs are native estimates, not billing reconciliation. Jev action
  input usage is priced at $0.042/million tokens; output is free
  ([official model pricing](https://docs.typesafe.ai/models), checked September 28). Unknown usage
  stays unknown. Jevgrep does not expose its charges to this wrapper, so retrieval
  comparisons show generative cost only and cannot establish total cost savings.
- Native events/transcripts and full manifests remain in the local study directory;
  committed results contain only public-fixture metadata and aggregate measurements.

Setup pilots are retained separately (alternatives-20260928 through v5). They
exposed missing lean-profile context/logging, final-message extraction requiring
raw event payloads, and a malformed experimental model-routing setting. These
were corrected before the v6 matched series. They are setup costs, excluded from
the repeated-task means, not discarded unsuccessful task solutions. Any completed
pilot inference is accounted separately in the final results. The same runtime
source hash is checked against receipts within every v6 run.
