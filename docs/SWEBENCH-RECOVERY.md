# SWE-bench campaign recovery

The 2,000-run Verified campaign stopped after 29 finished runs because one Jev
decision timed out and its usage could not be recovered. The agent's normal model
fallback completed its patch; the controller then stopped on unknown cost.

`budget_accounting.py` now permits continuation only for attributable Jev timeouts
with complete provider/retrieval usage and successful-judge receipts. It holds at
least $10 per unknown request against the existing $4,000 cap. The missing charge
remains null; a hold is not a measured cost. Known partial spending is retained.
Other missing usage, malformed evidence, backend errors, and infrastructure errors
still stop the campaign. Launch reservations include both known costs and holds.

After recovery, all four arms of the interrupted issue completed official grading.
The next issue exposed a second failure: Forge's spawn-helper lost its executable
bit. The shell never spawned, and no worker artifacts or native session existed.
Forge doctor repaired the helper. That failed launch was archived with its evidence
before retrying the same frozen run.

The launcher now runs one bounded doctor/retry only for the exact pre-spawn error
with no worker evidence. An observation timeout, existing worker files, session ID,
or ambiguous error never triggers this replay. Repeated launch failure stops.

The campaign directory preserves `harness-amendments.json`, the prior manifests,
`timeout-reconciliation.json`, `stop-investigation.json`, `launch-failures/`, and
the recomputable `budget-ledger.json`. Runtime source, prompts, model/thinking,
thresholds, completed patches and grades remain frozen. These are accounting and
launch-infrastructure amendments, not treatment changes.

The website displays known partial cost, unknown counts, and budget holds separately.
Completion still requires all 2,000 agent runs and official grades. The small
completed subset does not establish a savings conclusion.
